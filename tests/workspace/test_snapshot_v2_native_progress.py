"""真实 Snapshot v2 捕获段的可选进度端口；旧历史和编码仍在段外。"""

from __future__ import annotations

import ast
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.tools import workspace as read_module
from harnessix.workspace import snapshot as native_module
from harnessix.workspace import snapshot_v2 as module
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure


def _spread(root: Path, count: int, depth: int = 2):
    root.mkdir()
    requests = []
    for index in range(count):
        parent = root / f"d{index:03}"
        for level in range(depth - 1):
            parent /= f"p{level}"
        parent.mkdir(parents=True)
        leaf = parent / "file.txt"
        leaf.write_bytes(b"native facts\n")
        requests.append(
            WorkspaceResourceRequest(path=leaf.relative_to(root).as_posix(), access="read")
        )
    return tuple(requests)


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "root"
    requests = _spread(root, 4)
    blobs = {}
    snapshot = module.capture_workspace_snapshot_v2(
        root,
        resources=requests,
        checkpoint=lambda: None,
        write_blob=blobs.__setitem__,
        read_blob=blobs.__getitem__,
    )
    return root, snapshot, blobs


def _frozen_verify():
    directory = os.environ.get("HARNESSIX_NATIVE_FROZEN_DIR")
    if directory is None:
        pytest.skip("冻结旧实现 oracle 仅在显式提供原件时运行")
    path = Path(directory) / "src/harnessix/workspace/snapshot_v2.py"
    tree = ast.parse(path.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "verify_workspace_snapshot_v2"
    )
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function.name]


def test_native_context_wraps_only_real_capture_and_never_cas_or_encode(case, monkeypatch):
    root, snapshot, blobs = case
    before = dict(blobs)
    trace, active = [], False

    def full():
        assert not active
        trace.append("full")

    def local():
        assert active
        trace.append("local")

    @contextmanager
    def progress():
        nonlocal active
        trace.append("enter")
        active = True
        try:
            yield local
        finally:
            active = False
            trace.append("exit")

    def read(digest):
        assert not active
        trace.append("cas")
        return blobs[digest]

    original = module._encode_snapshot

    def encode(facts, checkpoint):
        assert not active
        assert checkpoint is full
        trace.append("encode")
        return original(facts, checkpoint)

    monkeypatch.setattr(module, "_encode_snapshot", encode)
    assert (
        module.verify_workspace_snapshot_v2(
            snapshot,
            root,
            checkpoint=full,
            read_blob=read,
            native_progress=progress(),
        )
        == snapshot
    )
    assert trace.index("cas") < trace.index("enter") < trace.index("local")
    assert trace.index("local") < trace.index("exit") < trace.index("encode")
    assert "cas" not in trace[trace.index("enter") : trace.index("exit")]
    assert before == blobs


@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_callback_has_complete_frozen_trace_and_wire(case, monkeypatch, explicit_none):
    root, snapshot, blobs = case
    frozen = _frozen_verify()
    trace = []
    original = native_module._PosixRoot.observe

    def observe(native, path, *, access, checkpoint=None):
        trace.append(("native", path, access))
        return original(native, path, access=access, checkpoint=checkpoint)

    monkeypatch.setattr(native_module._PosixRoot, "observe", observe)

    def check():
        trace.append("check")

    def read(digest):
        trace.append(("cas", digest))
        return blobs[digest]

    expected = frozen(snapshot, root, checkpoint=check, read_blob=read)
    old_trace = tuple(trace)
    trace.clear()
    kwargs = {"native_progress": None} if explicit_none else {}
    actual = module.verify_workspace_snapshot_v2(
        snapshot,
        root,
        checkpoint=check,
        read_blob=read,
        **kwargs,
    )
    assert tuple(trace) == old_trace
    assert actual.model_dump_json() == expected.model_dump_json()


@pytest.mark.skipif(os.name != "posix", reason="实际 POSIX 原生观察")
@pytest.mark.parametrize("count,depth", [(8, 3), (255, 4), (1, 127)])
def test_real_native_maximum_scope_readonly_store_reopen(tmp_path, count, depth):
    root = tmp_path / "root"
    requests = _spread(root, count, depth)
    state = tmp_path / "cas"
    with SQLiteWorkspaceTransactionStore(state) as store:
        snapshot = module.capture_workspace_snapshot_v2(
            root,
            resources=requests,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        assert len(snapshot.resources) == count + 1
        assert snapshot.parent_closure.parent_count == count * depth + 1
        before = {item.name: item.read_bytes() for item in store._blobs.iterdir()}
    calls = []

    @contextmanager
    def progress():
        calls.append("enter")
        yield lambda: calls.append("local")
        calls.append("exit")

    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        changes = store._db.total_changes
        assert (
            module.verify_workspace_snapshot_v2(
                snapshot,
                root,
                checkpoint=lambda: calls.append("full"),
                read_blob=store._read_blob,
                native_progress=progress(),
            )
            == snapshot
        )
        assert store._db.total_changes == changes == 0
        assert before == {item.name: item.read_bytes() for item in store._blobs.iterdir()}
        parents = read_workspace_parent_closure(snapshot, store._read_blob, checkpoint=lambda: None)
        assert len(parents) == count * depth + 1
    assert "local" in calls and calls.index("enter") < calls.index("exit")
    assert all((root / item.path).read_bytes() == b"native facts\n" for item in requests)


def test_native_port_preserves_all_sixteen_external_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    external = {}
    requests = []
    for index in range(16):
        path = tmp_path / f"external{index}"
        path.mkdir()
        (path / "file").write_bytes(b"external\n")
        location = f"ext{index}"
        external[location] = (path, ("read",))
        requests.append(WorkspaceResourceRequest(location=location, path="file", access="read"))
    blobs = {}
    snapshot = module.capture_workspace_snapshot_v2(
        root,
        resources=requests,
        external_roots=external,
        checkpoint=lambda: None,
        write_blob=blobs.__setitem__,
        read_blob=blobs.__getitem__,
    )

    @contextmanager
    def progress():
        yield lambda: None

    assert (
        module.verify_workspace_snapshot_v2(
            snapshot,
            root,
            external_roots=external,
            checkpoint=lambda: None,
            read_blob=blobs.__getitem__,
            native_progress=progress(),
        )
        == snapshot
    )
    assert len(snapshot.external_roots) == 16 and snapshot.parent_closure.parent_count == 17


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_port_only_changes_checkpoint_not_capture_arguments(case, monkeypatch, platform):
    """平台字段结构传递，不代表 Windows 原生验收。"""
    root, snapshot, _ = case
    snapshot = snapshot.model_copy(update={"platform": platform})
    historical = object()
    observed = {}
    external = {"cache": (root, ("read",))}
    facts = SimpleNamespace(parents=historical)

    def local():
        pass

    def full():
        pass

    @contextmanager
    def progress():
        yield local

    def history(expected, read_blob, *, checkpoint):
        assert checkpoint is full
        return historical

    def capture(selected_root, **kwargs):
        observed.update(root=selected_root, **kwargs)
        return facts

    monkeypatch.setattr(module, "read_workspace_parent_closure", history)
    monkeypatch.setattr(module, "capture_snapshot_facts", capture)
    monkeypatch.setattr(module, "_encode_snapshot", lambda *_: (snapshot, ()))
    assert (
        module.verify_workspace_snapshot_v2(
            snapshot,
            root,
            checkpoint=full,
            read_blob=lambda _: b"",
            external_roots=external,
            native_progress=progress(),
        )
        is snapshot
    )
    assert observed == {
        "root": root,
        "cwd": snapshot.cwd,
        "resources": module.snapshot_requests(snapshot),
        "external_roots": external,
        "platform": platform,
        "checkpoint": local,
    }


@pytest.mark.parametrize("failure", ["stopped", "deadline"])
def test_native_read_operation_stop_and_deadline_remain_active(case, monkeypatch, failure):
    root, snapshot, blobs = case
    original = NativeReadOperation.__init__
    operations = []

    def initialize(operation, checkpoint):
        original(operation, checkpoint)
        operations.append(operation)
        if failure == "stopped":
            operation.stopped.set()
        else:
            operation.deadline = read_module.time.monotonic() - 1

    monkeypatch.setattr(NativeReadOperation, "__init__", initialize)
    monkeypatch.setattr(module, "_encode_snapshot", lambda *_: pytest.fail("不能提前编码"))

    @contextmanager
    def progress():
        yield lambda: None

    error_type = TurnCancelled if failure == "stopped" else KernelError
    with pytest.raises(error_type) as caught:
        module.verify_workspace_snapshot_v2(
            snapshot,
            root,
            checkpoint=lambda: None,
            read_blob=blobs.__getitem__,
            native_progress=progress(),
        )
    assert operations
    if failure == "deadline":
        assert caught.value.code == "workspace_timeout"


@pytest.mark.parametrize("failure", ["symlink", "changed-parent", "changed-leaf"])
def test_original_native_protection_and_stale_comparison_remain(case, failure):
    root, snapshot, blobs = case
    before = dict(blobs)
    leaf = root / snapshot.resources[-1].path
    if failure == "symlink":
        leaf.unlink()
        leaf.symlink_to(root / "d000/p0/file.txt")
    elif failure == "changed-parent":
        (leaf.parent / "extra").write_bytes(b"new member")
    else:
        leaf.write_bytes(b"later facts")

    @contextmanager
    def progress():
        yield lambda: None

    with pytest.raises(KernelError) as caught:
        module.verify_workspace_snapshot_v2(
            snapshot,
            root,
            checkpoint=lambda: None,
            read_blob=blobs.__getitem__,
            native_progress=progress(),
        )
    assert caught.value.code == (
        "workspace_path_denied" if failure == "symlink" else "execution_plan_stale"
    )
    assert blobs == before


def test_workspace_port_does_not_unwrap_context_owned_exceptions(case):
    root, snapshot, blobs = case
    nested = UpstreamCheckpointError(UpstreamCheckpointError(OSError("original")))

    @contextmanager
    def progress():
        def check():
            raise nested

        yield check

    with pytest.raises(UpstreamCheckpointError) as caught:
        module.verify_workspace_snapshot_v2(
            snapshot,
            root,
            checkpoint=lambda: None,
            read_blob=blobs.__getitem__,
            native_progress=progress(),
        )
    assert caught.value is nested
