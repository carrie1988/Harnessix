"""根身份鲜读不枚举成员；完整 Snapshot 的内容与父历史验真仍是独立边界。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config.git_baseline import _root_binding_matches
from harnessix.tools.workspace import Workspace
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_capture import capture_workspace_binding
from harnessix.workspace.snapshot_v2 import (
    capture_workspace_snapshot_v2,
    verify_workspace_snapshot_v2,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX fresh root binding")


def _control():
    return GitAuthenticationControl(lambda: None, lambda: None)


def _source(root: Path):
    snapshot = capture_workspace_snapshot(root)
    return SimpleNamespace(workspace=snapshot)


@pytest.mark.parametrize("depth", [0, 25, 127])
def test_binding_has_original_scope_without_member_enumeration(tmp_path, monkeypatch, depth):
    root = tmp_path.joinpath(*(["d"] * depth))
    root.mkdir(parents=True, exist_ok=True)
    (root / "unchanged.txt").write_bytes(b"body")
    source = _source(root)
    opens = []
    original = Workspace._open_root

    def open_root(workspace):
        opens.append(1)
        return original(workspace)

    monkeypatch.setattr(Workspace, "_open_root", open_root)
    monkeypatch.setattr(os, "scandir", lambda *_: pytest.fail("根身份不得枚举成员"))
    assert _root_binding_matches(source, root, _control())
    assert len(opens) == 3  # 新根能力、读前/读后路径鲜读；不复用上一检查点的身份。
    assert _root_binding_matches(source, root, _control())
    assert len(opens) == 6


@pytest.mark.parametrize("replace_ancestor", [False, True])
def test_replacement_between_bindings_is_not_cached(tmp_path, replace_ancestor):
    parent = tmp_path / "parent"
    root = parent / "root"
    root.mkdir(parents=True)
    source = _source(root)
    assert _root_binding_matches(source, root, _control())
    target = parent if replace_ancestor else root
    target.rename(tmp_path / "old")
    root.mkdir(parents=True)
    assert not _root_binding_matches(source, root, _control())


@pytest.mark.parametrize("replace_ancestor", [False, True])
def test_root_path_drift_during_binding_is_rejected(tmp_path, replace_ancestor):
    parent = tmp_path / "parent"
    root = parent / "root"
    root.mkdir(parents=True)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            (parent if replace_ancestor else root).rename(tmp_path / "original")
            root.mkdir(parents=True)

    with pytest.raises(KernelError) as caught:
        capture_workspace_binding(root, platform="posix", checkpoint=check)
    assert caught.value.code == "workspace_workspace_changed"


@pytest.mark.parametrize("phase", [1, 2, 3])
@pytest.mark.parametrize("kind", ["cancel", "timeout", "same-code", "os"])
def test_first_control_error_and_descriptor_cleanup(tmp_path, phase, kind, monkeypatch):
    source = _source(tmp_path)
    error = {
        "cancel": TurnCancelled(),
        "timeout": TimeoutError(),
        "same-code": KernelError("workspace_observation_failed", "control"),
        "os": OSError("control"),
    }[kind]
    descriptors = set()
    open_original, close_original = os.open, os.close

    def opened(*args, **kwargs):
        descriptor = open_original(*args, **kwargs)
        descriptors.add(descriptor)
        return descriptor

    def closed(descriptor):
        close_original(descriptor)
        descriptors.discard(descriptor)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == phase:
            raise error

    with pytest.raises(BaseException) as caught:
        _root_binding_matches(source, tmp_path, check)
    assert caught.value is error and calls == phase
    assert descriptors == set()


def test_exact_control_retains_full_entry_exit_and_local_cancel(tmp_path):
    source = _source(tmp_path)
    trace = []
    control = GitAuthenticationControl(lambda: trace.append("local"), lambda: trace.append("full"))
    assert _root_binding_matches(source, tmp_path, control)
    assert trace[0] == trace[-1] == "full" and "local" in trace


@pytest.mark.parametrize("changed", ["file", "parent-member"])
def test_binding_does_not_replace_full_resource_verification(tmp_path, changed):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src/file.py"
    target.write_bytes(b"before")
    blobs = {}
    source = SimpleNamespace(
        workspace=capture_workspace_snapshot_v2(
            tmp_path,
            resources=(WorkspaceResourceRequest(path="src/file.py", access="read"),),
            checkpoint=lambda: None,
            write_blob=blobs.__setitem__,
            read_blob=blobs.__getitem__,
        )
    )
    if changed == "file":
        target.write_bytes(b"after")
    else:
        (tmp_path / "src/new.py").write_bytes(b"new")
    assert _root_binding_matches(source, tmp_path, _control())
    with pytest.raises(KernelError) as caught:
        verify_workspace_snapshot_v2(
            source.workspace, tmp_path, checkpoint=lambda: None, read_blob=blobs.__getitem__
        )
    assert caught.value.code == "execution_plan_stale"
