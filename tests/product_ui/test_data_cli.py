from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.product_config.state_owner import product_state_owner
from harnessix.product_ui.data_cli import (
    _all_threads,
    _thread_events,
    _write_private_json,
    delete_workspace_state,
    list_workspaces,
    thread_diagnostics,
)
from harnessix.product_ui.errors import ProductUIError
from harnessix.protocol.contracts import (
    PublicBudget,
    PublicFailure,
    PublicUsage,
    ThreadListResult,
    ThreadView,
    TurnView,
)

_FINGERPRINT = "a" * 64


def _state(root: Path, name: str = _FINGERPRINT) -> Path:
    target = root / name
    (target / "runtime").mkdir(parents=True, mode=0o700)
    (target / "runtime" / "sessions.db").write_bytes(b"12345")
    return target


def test_list_reports_size_without_reading_content(tmp_path: Path) -> None:
    _state(tmp_path)

    entries = list_workspaces(tmp_path)

    assert [item["fingerprint"] for item in entries] == [_FINGERPRINT]
    assert entries[0]["size_bytes"] == 5
    assert list_workspaces(tmp_path / "missing") == []


def test_delete_requires_matching_confirmation(tmp_path: Path) -> None:
    target = _state(tmp_path)

    with pytest.raises(ProductUIError) as refused:
        delete_workspace_state(tmp_path, _FINGERPRINT, confirm="b" * 64)

    assert refused.value.code == "data_delete_unconfirmed" and target.is_dir()
    delete_workspace_state(tmp_path, _FINGERPRINT, confirm=_FINGERPRINT)
    assert not target.exists()


def test_delete_refuses_a_running_workspace(tmp_path: Path) -> None:
    target = _state(tmp_path)

    with product_state_owner(target / "runtime"):
        with pytest.raises(ProductUIError) as busy:
            delete_workspace_state(tmp_path, _FINGERPRINT, confirm=_FINGERPRINT)

    assert busy.value.code == "data_workspace_busy" and target.is_dir()


@pytest.mark.parametrize("name", ["../escape", "missing"])
def test_delete_refuses_paths_outside_the_root(tmp_path: Path, name: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "escape").mkdir()

    with pytest.raises(ProductUIError) as missing:
        delete_workspace_state(root, name, confirm=name)

    assert missing.value.code == "data_workspace_missing"
    assert (tmp_path / "escape").is_dir()


def test_delete_refuses_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (root / _FINGERPRINT).symlink_to(other, target_is_directory=True)

    with pytest.raises(ProductUIError):
        delete_workspace_state(root, _FINGERPRINT, confirm=_FINGERPRINT)

    assert other.is_dir()


def _thread(*, error: bool = False) -> ThreadView:
    now = datetime.now(UTC)
    return ThreadView(
        thread_id=uuid4(),
        workspace="/secret/path",
        cursor=3,
        turn_count=1,
        latest_turn=TurnView(
            turn_id=uuid4(),
            request_id="secret-request",
            status="failed" if error else "completed",
            budget=PublicBudget(
                max_steps=1,
                max_tokens=1,
                timeout_seconds=1.0,
                max_output_chars=1,
                max_tool_calls_per_step=1,
            ),
            usage=PublicUsage(input_tokens=4, output_tokens=2, total_tokens=6),
            model_steps=2,
            error=(
                PublicFailure(
                    code="provider_transport",
                    message="包含路径 /secret/path",
                    retryable=True,
                    category="provider",
                )
                if error
                else None
            ),
            created_at=now,
        ),
        created_at=now,
        updated_at=now,
    )


def test_diagnostics_keep_codes_and_drop_content() -> None:
    summary = json.dumps(thread_diagnostics(_thread(error=True)), ensure_ascii=False)

    assert "provider_transport" in summary and '"model_steps": 2' in summary
    assert "/secret/path" not in summary and "secret-request" not in summary


async def test_export_pages_through_threads_and_events() -> None:
    threads = [_thread(), _thread()]

    class Client:
        async def list_threads(self, *, cursor, limit, archived):
            if archived:
                return ThreadListResult(threads=(threads[1],))
            if cursor is None:
                return ThreadListResult(threads=(threads[0],), next_cursor="next")
            return ThreadListResult(threads=())

        async def replay_events(self, _thread_id, *, after_cursor, limit):
            event = SimpleNamespace(model_dump=lambda **_: {"cursor": after_cursor + 1})
            return SimpleNamespace(
                events=(event,), scanned_through=after_cursor + 1, has_more=after_cursor < 1
            )

    client = Client()

    assert await _all_threads(client) == threads  # type: ignore[arg-type]
    events = await _thread_events(client, threads[0])  # type: ignore[arg-type]
    assert [item["cursor"] for item in events] == [1, 2]


def test_output_is_private_and_never_overwrites(tmp_path: Path) -> None:
    output = tmp_path / "export.json"

    _write_private_json(output, {"a": 1})

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        _write_private_json(output, {"a": 2})
