from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import planner
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction


@pytest.mark.skipif(not hasattr(Path, "chmod"), reason="需要本地文件系统")
def test_prepares_complete_multi_file_plan_and_private_blob_set(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    keep = tmp_path / "src/keep.py"
    keep.write_bytes(b"old\n")
    keep.chmod(0o644)
    remove = tmp_path / "remove.txt"
    remove.write_bytes(b"remove\n")
    remove.chmod(0o644)

    prepared = prepare_workspace_transaction(
        tmp_path,
        {
            "src/new.py": DesiredWorkspaceFile(b"new\n", 0o644),
            "src/keep.py": DesiredWorkspaceFile(b"changed\n", 0o755),
            "remove.txt": DesiredWorkspaceFile(None),
        },
        request_id="delivery-1",
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )

    assert [item.path for item in prepared.plan.mutations] == [
        "remove.txt",
        "src/keep.py",
        "src/new.py",
    ]
    assert prepared.plan.mutations[0].after.presence == "absent"
    assert prepared.plan.mutations[1].before.sha256 in prepared.blobs
    assert prepared.plan.mutations[1].after.mode == 0o755
    assert prepared.plan.mutations[2].before.presence == "absent"
    resources = {(item.path, item.access, item.kind) for item in prepared.plan.source.resources}
    assert ("src", "read", "directory") in resources
    assert ("src/new.py", "write", "missing") in resources


@pytest.mark.parametrize(
    "path",
    [".git/config", ".harnessix/state", ".codex/settings.json", ".agents/rules", ".env"],
)
def test_planner_rejects_control_plane_and_secret_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(KernelError) as denied:
        prepare_workspace_transaction(
            tmp_path,
            {path: DesiredWorkspaceFile(b"x", 0o644)},
            request_id="denied",
        )
    assert denied.value.code == "delivery_path_denied"


def test_planner_detects_source_change_during_final_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    original = planner.verify_workspace_snapshot

    def drift_then_verify(*args: object, **kwargs: object):
        target.write_bytes(b"raced")
        return original(*args, **kwargs)

    monkeypatch.setattr(planner, "verify_workspace_snapshot", drift_then_verify)
    with pytest.raises(KernelError) as changed:
        prepare_workspace_transaction(
            tmp_path,
            {"target.txt": DesiredWorkspaceFile(b"after", 0o644)},
            request_id="changed",
        )
    assert changed.value.code == "execution_plan_stale"
