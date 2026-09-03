from __future__ import annotations

import asyncio
import os
from threading import Event

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import planner
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.planner import prepare_patch, verify_prepared
from harnessix.tools.workspace import ReadOperation, Workspace, digest, revision_state
from tests.patches.test_planner import proposal


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "fifo", "denied", "missing", "special_mode"]
)
def test_file_capability_rejections(tmp_path, kind):
    path = tmp_path / "main.py"
    if kind == "symlink":
        (tmp_path / "target").write_text("before")
        path.symlink_to(tmp_path / "target")
    elif kind == "hardlink":
        (tmp_path / "target").write_text("before")
        os.link(tmp_path / "target", path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind != "missing":
        path.write_text("before")
    if kind == "special_mode":
        path.chmod(0o4644)
    with Workspace(tmp_path, denied_paths=("main.py",) if kind == "denied" else ()) as workspace:
        revision = (
            digest((workspace.scope, "main.py", revision_state(path.stat())))
            if path.exists()
            else "0" * 64
        )
        args = PatchProposal(
            path="main.py",
            expected_revision=revision,
            edits=(ExactEdit(old_text="before", new_text="after"),),
        )
        with pytest.raises(KernelError) as error:
            prepare_patch(workspace, args, ReadOperation())
    assert (
        error.value.code
        == {
            "symlink": "patch_path_denied",
            "hardlink": "patch_path_denied",
            "directory": "patch_wrong_file_type",
            "fifo": "patch_wrong_file_type",
            "denied": "patch_path_denied",
            "missing": "patch_not_found",
            "special_mode": "patch_unsupported_mode",
        }[kind]
    )


@pytest.mark.parametrize("change", ["content", "file", "root", "io", "growth"])
def test_race_or_failure_during_full_read_releases_fd(tmp_path, monkeypatch, change):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "main.py"
    path.write_text("before")
    original_read = os.read
    seen = []
    with Workspace(root) as workspace:
        args = proposal(workspace)

        def read(fd, size):
            if not seen:
                seen.append(fd)
                if change == "content":
                    path.write_text("change")
                elif change == "file":
                    (root / "new").write_text("before")
                    (root / "new").replace(path)
                elif change == "root":
                    root.rename(tmp_path / "old")
                    root.mkdir()
                elif change == "io":
                    raise OSError("PRIVATE full read")
                elif change == "growth":
                    path.write_text("beforeX")
            return original_read(fd, size)

        if change == "growth":
            monkeypatch.setattr(planner, "MAX_PATCH_BYTES", 6)
        monkeypatch.setattr(os, "read", read)
        with pytest.raises(KernelError) as error:
            prepare_patch(workspace, args, ReadOperation())
        assert error.value.code == (
            "patch_io_failed"
            if change == "io"
            else "patch_limit_exceeded"
            if change == "growth"
            else "patch_workspace_changed"
        )
        assert "PRIVATE" not in str(error.value)
        with pytest.raises(OSError):
            os.fstat(seen[0])


async def test_cancel_inflight_host_thread_is_joined_and_releases_read_fd(tmp_path, monkeypatch):
    path = tmp_path / "main.py"
    path.write_text("before")
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original = os.read
    seen = []

    def read(fd, size):
        seen.append(fd)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original(fd, size)

    with Workspace(tmp_path) as workspace:
        args, operation = proposal(workspace), ReadOperation()
        monkeypatch.setattr(os, "read", read)
        worker = asyncio.create_task(asyncio.to_thread(prepare_patch, workspace, args, operation))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            operation.stopped.set()
            release.set()
            with pytest.raises(TurnCancelled):
                await asyncio.wait_for(worker, 10)
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
        with pytest.raises(OSError):
            os.fstat(seen[0])
    assert path.read_text() == "before"


def test_verify_timeout_is_not_mislabeled_as_corrupt(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("before")
    with Workspace(tmp_path) as workspace:
        prepared = prepare_patch(workspace, proposal(workspace), ReadOperation())
        original = planner._target

        def timeout(before, proposal, operation):
            operation.deadline = 0
            return original(before, proposal, operation)

        monkeypatch.setattr(planner, "_target", timeout)
        with pytest.raises(KernelError) as error:
            verify_prepared(workspace, prepared, ReadOperation())
        assert error.value.code == "patch_timeout"


@pytest.mark.parametrize(
    "path", ["/tmp/x", "../x", "a/../b", "./x", ".", "a//b", "a\\b", "a\x00b", "中" * 342]
)
def test_proposal_rejects_unsafe_path_before_io(path):
    with pytest.raises(ValidationError):
        PatchProposal(
            path=path, expected_revision="0" * 64, edits=(ExactEdit(old_text="a", new_text="b"),)
        )


@pytest.mark.parametrize(
    "old,new",
    [("", "a"), ("a", "a"), ("a", "\x00"), ("\x7f", "b"), ("a", "\ud800"), ("中" * 44000, "x")],
)
def test_exact_edit_is_strict_and_byte_bounded(old, new):
    with pytest.raises(ValidationError):
        ExactEdit(old_text=old, new_text=new)


@pytest.mark.parametrize("change", ["extra", "revision", "empty", "many", "total"])
def test_proposal_rejects_invalid_or_unbounded_changes(change):
    data = {
        "path": "main.py",
        "expected_revision": "0" * 64,
        "edits": (ExactEdit(old_text="a", new_text="b"),),
    }
    if change == "extra":
        data["thread_id"] = "host-only"
    elif change == "revision":
        data["expected_revision"] = None
    elif change == "empty":
        data["edits"] = ()
    elif change == "many":
        data["edits"] *= 33
    else:
        data["edits"] = (ExactEdit(old_text="a" * 65536, new_text="b" * 65536),) * 3
    with pytest.raises(ValidationError):
        PatchProposal(**data)
