from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from harnessix.product_ui.workspace_trust import (
    TRUST_ENVIRONMENT,
    confirm_workspace_trust,
    is_trusted,
    remember_trust,
)


def _confirm(workspace: Path, store: Path, **options: object) -> bool:
    values: dict[str, object] = {"environ": {}, "interactive": True, **options}
    return confirm_workspace_trust(workspace, store=store, **values)  # type: ignore[arg-type]


def test_confirmation_is_remembered_and_covers_subdirectories(tmp_path: Path) -> None:
    store = tmp_path / "home" / "trusted.json"
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)

    assert _confirm(workspace, store, read_answer=lambda _prompt: "y")

    assert is_trusted(workspace, store) and is_trusted(workspace / "src", store)
    assert not is_trusted(tmp_path, store)
    # Windows以ACL保护私有文件，st_mode不表示访问权限。
    if os.name == "posix":
        assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert _confirm(workspace, store, read_answer=lambda _prompt: pytest.fail("不应再次询问"))


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_declining_is_not_remembered(tmp_path: Path, answer: str) -> None:
    store = tmp_path / "trusted.json"

    assert not _confirm(tmp_path, store, read_answer=lambda _prompt: answer)
    assert not store.exists()


def test_interrupt_counts_as_declining(tmp_path: Path) -> None:
    def interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    assert not _confirm(tmp_path, tmp_path / "trusted.json", read_answer=interrupted)


def test_non_interactive_run_needs_explicit_trust(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "trusted.json"

    assert not _confirm(tmp_path, store, interactive=False)
    assert "--trust-workspace" in capsys.readouterr().err
    assert _confirm(tmp_path, store, interactive=False, assume_trusted=True)
    assert _confirm(tmp_path, store, interactive=False, environ={TRUST_ENVIRONMENT: "1"})
    assert not store.exists()


@pytest.mark.parametrize(
    "body",
    ["not json", "[]", '{"spec_version": "other", "workspaces": ["/"]}', '{"spec_version": 1}'],
)
def test_corrupt_store_grants_no_trust(tmp_path: Path, body: str) -> None:
    store = tmp_path / "trusted.json"
    store.write_text(body, encoding="utf-8")

    assert not is_trusted(tmp_path, store)


def test_remember_keeps_existing_entries(tmp_path: Path) -> None:
    store = tmp_path / "trusted.json"
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()

    remember_trust(first, store)
    remember_trust(second, store)

    assert len(json.loads(store.read_text(encoding="utf-8"))["workspaces"]) == 2
