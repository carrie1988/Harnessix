from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.context.sources import ContextSourceError
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.tools.windows_read import WindowsReadRuntime
from tests.context.test_sources import request, sourced
from tests.tools.test_windows_read_adapter import _FakeWindowsRoot


@pytest.fixture
def windows_readers(monkeypatch: pytest.MonkeyPatch) -> list[WindowsReadRuntime]:
    readers: list[WindowsReadRuntime] = []

    def open_reader(root: Path, denied_paths: tuple[str, ...]) -> WindowsReadRuntime:
        reader = WindowsReadRuntime(root, denied_paths=denied_paths, root_factory=_FakeWindowsRoot)
        readers.append(reader)
        return reader

    monkeypatch.setattr("harnessix.context.read_workspace._open_reader", open_reader)
    return readers


async def test_windows_project_override_and_missing_candidates_use_native_port(
    tmp_path: Path, windows_readers: list[WindowsReadRuntime]
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "AGENTS.md").write_text("根规则", encoding="utf-8")
    (tmp_path / "src/AGENTS.override.md").write_text("子目录覆盖规则", encoding="utf-8")
    (tmp_path / "src/AGENTS.md").write_text("不能被选中的规则", encoding="utf-8")
    prepared = await sourced(tmp_path, working_directory="src").prepare(
        request(tmp_path), CancelToken()
    )
    fragments = json.loads(prepared.instructions or "")["fragments"]
    assert [item["source"] for item in fragments if item["kind"] == "project_instruction"] == [
        "AGENTS.md",
        "src/AGENTS.override.md",
    ]
    assert windows_readers and all(reader._root.closed for reader in windows_readers)


async def test_windows_product_sources_share_scope_and_keep_sensitive_files_hidden(
    tmp_path: Path, windows_readers: list[WindowsReadRuntime]
) -> None:
    (tmp_path / "AGENTS.md").write_text("项目规范", encoding="utf-8")
    (tmp_path / ".env").write_text("secret-canary", encoding="utf-8")
    prepared = await build_product_agent_context(tmp_path).context.prepare(
        request(tmp_path), CancelToken()
    )
    assert prepared.inspection.consistency is not None
    assert prepared.inspection.consistency.source_count == 3
    assert len({item.workspace_scope for item in prepared.inspection.sources}) == 1
    assert "secret-canary" not in (prepared.instructions or "")
    assert '".env"' not in (prepared.instructions or "")
    assert windows_readers and all(reader._root.closed for reader in windows_readers)


@pytest.mark.parametrize("unsafe", ["hardlink", "binary", "oversized"])
async def test_windows_unsafe_instruction_fails_closed(
    tmp_path: Path, windows_readers: list[WindowsReadRuntime], unsafe: str
) -> None:
    path = tmp_path / "AGENTS.md"
    if unsafe == "hardlink":
        original = tmp_path / "original"
        original.write_text("不能读取", encoding="utf-8")
        os.link(original, path)
    elif unsafe == "binary":
        path.write_bytes(b"a\0b")
    else:
        path.write_text("x\n" * 33_000, encoding="utf-8")
    with pytest.raises(ContextSourceError) as error:
        await build_product_agent_context(tmp_path).context.prepare(
            request(tmp_path), CancelToken()
        )
    assert error.value.code in {"context_source_invalid", "context_source_too_large"}
    assert windows_readers and all(reader._root.closed for reader in windows_readers)


async def test_windows_cancelled_source_does_not_open_native_handle(
    tmp_path: Path, windows_readers: list[WindowsReadRuntime]
) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(TurnCancelled):
        await build_product_agent_context(tmp_path).context.prepare(request(tmp_path), token)
    assert not windows_readers


@pytest.mark.skipif(os.name != "nt", reason="真实Handle观察需要Windows原生宿主")
async def test_product_context_on_native_windows_handle(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("原生Windows项目规范", encoding="utf-8")
    prepared = await build_product_agent_context(tmp_path).context.prepare(
        request(tmp_path), CancelToken()
    )
    assert "原生Windows项目规范" in (prepared.instructions or "")
    assert all(item.workspace_scope for item in prepared.inspection.sources)
