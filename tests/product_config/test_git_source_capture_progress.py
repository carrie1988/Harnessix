"""Source真实采集接线：原生/编码/文件读取分层，CAS仍完整认证。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceFileVersion
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import git_delivery_source as module
from harnessix.workspace import snapshot_v2 as snapshots
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.workspace.test_snapshot_v2_native_progress import case as case


class CaptureProbe:
    """只观测原调用；所有目录、编码及CAS均委托原实现。"""

    def __init__(self):
        self.phase = "outside"
        self.trace = []
        self.collecting = False
        self.failure = None
        self.error = None
        self.saved = None
        self.control = GitAuthenticationControl(self.local, self.full)

    def record(self, mode):
        self.trace.append((mode, self.phase))
        if self.failure == (mode, self.phase):
            raise self.error

    def full(self):
        self.record("full")

    def local(self):
        self.record("local")


@pytest.fixture
def capture_case(case, monkeypatch):
    root, base, blobs = case
    versions = {}
    for resource in base.resources:
        if resource.kind == "file":
            body, mode = module._read_existing(root, resource.path, base.platform)
            after = WorkspaceFileVersion(
                presence="file", sha256=hashlib.sha256(body).hexdigest(), size=len(body), mode=mode
            )
            versions[resource.path] = (WorkspaceFileVersion(presence="absent", size=0), after)
    probe = CaptureProbe()
    original_capture = snapshots.capture_snapshot_facts
    original_encode = snapshots._encode_snapshot
    original_history = snapshots.read_workspace_parent_closure
    original_read = module._read_existing
    original_verify = module._verify_final_snapshot

    def capture(*args, **kwargs):
        if not probe.collecting:
            return original_capture(*args, **kwargs)
        probe.phase, probe.saved = "native", kwargs["checkpoint"]
        try:
            return original_capture(*args, **kwargs)
        finally:
            probe.phase = "capture-exit"

    def encode(*args, **kwargs):
        if not probe.collecting:
            return original_encode(*args, **kwargs)
        probe.phase = "encode"
        try:
            return original_encode(*args, **kwargs)
        finally:
            probe.phase = "encoded"

    def history(*args, **kwargs):
        if not probe.collecting:
            return original_history(*args, **kwargs)
        probe.phase = "history"
        try:
            return original_history(*args, **kwargs)
        finally:
            probe.phase = "history-end"

    def snapshot(*args, **kwargs):
        probe.collecting, probe.phase = True, "capture-entry"
        try:
            return snapshots.capture_workspace_snapshot_v2(*args, **kwargs)
        finally:
            probe.collecting, probe.phase = False, "outside"

    def write(digest, body):
        probe.phase = "cas-write"
        probe.record("write")
        blobs[digest] = body
        probe.phase = "after-write"

    def read(digest):
        previous = probe.phase
        probe.phase = "cas-read"
        probe.record("read")
        body = blobs[digest]
        probe.phase = "after-read" if probe.collecting else previous
        return body

    def file_read(*args, **kwargs):
        probe.phase = "file"
        return original_read(*args, **kwargs)

    def verify(*args, **kwargs):
        probe.phase = "verify"
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(snapshots, "capture_snapshot_facts", capture)
    monkeypatch.setattr(snapshots, "_encode_snapshot", encode)
    monkeypatch.setattr(snapshots, "read_workspace_parent_closure", history)
    monkeypatch.setattr(module, "capture_workspace_snapshot_v2", snapshot)
    monkeypatch.setattr(module, "_read_existing", file_read)
    monkeypatch.setattr(module, "_verify_final_snapshot", verify)
    ports = WorkspaceSnapshotPorts(write, read)
    return SimpleNamespace(
        root=root, base=base, blobs=blobs, versions=versions, probe=probe, ports=ports
    )


def _run(case, callback, implementation=module._observe_final_versions):
    case.probe.phase = "outside"
    return implementation(case.root, case.base, case.versions, callback, case.ports)


def _frozen():
    path = Path(__file__).with_name("fixtures") / "git_source_capture_before.txt"
    body = path.read_bytes()
    assert (
        hashlib.sha256(body).hexdigest()
        == "a2323272d8fc928585f741e02e77eed92ae96b42f24f36b5478600d89b97e95a"
    )
    namespace = dict(vars(module))
    exec(compile(body, str(path), "exec"), namespace)
    return namespace["_observe_final_versions"]


def test_real_exact_capture_layers_native_encode_and_files_but_keeps_all_cas_full(capture_case):
    case, probe = capture_case, capture_case.probe
    before = dict(case.blobs)
    result = _run(case, probe.control)
    assert result == case.base and case.blobs == before
    assert ("local", "native") in probe.trace and ("local", "encode") in probe.trace
    assert ("full", "capture-entry") in probe.trace and ("full", "capture-exit") in probe.trace
    assert ("full", "encoded") in probe.trace
    assert ("local", "cas-write") not in probe.trace and ("local", "cas-read") not in probe.trace
    assert ("local", "file") in probe.trace and ("full", "file") in probe.trace
    assert sum(mode == "write" for mode, _ in probe.trace) == 2
    assert sum(mode == "read" for mode, _ in probe.trace) == 6
    for index, (mode, _) in enumerate(probe.trace):
        if mode in {"read", "write"}:
            assert probe.trace[index - 1][0] == probe.trace[index + 1][0] == "full"
    probe.trace.clear()
    probe.saved()
    assert probe.trace == [("full", "verify")]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_capture_keeps_complete_old_full_trace_and_wire(capture_case, kind):
    case, probe = capture_case, capture_case.probe

    class Proxy:
        def __call__(self):
            probe.full()

    class Subclass(GitAuthenticationControl):
        pass

    callback = {
        "function": probe.full,
        "proxy": Proxy(),
        "subclass": Subclass(probe.local, probe.full),
    }[kind]
    expected = _run(case, callback, _frozen())
    trace, bodies = tuple(probe.trace), dict(case.blobs)
    probe.trace.clear()
    assert _run(case, callback).model_dump_json() == expected.model_dump_json()
    assert tuple(probe.trace) == trace and case.blobs == bodies
    assert all(mode != "local" for mode, _ in trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["task", "thread"])
async def test_actual_foreign_capture_keeps_old_full_trace_without_added_authentication(
    capture_case, owner
):
    case, probe = capture_case, capture_case.probe
    control = GitAuthenticationControl(probe.local, probe.full)

    async def invoke(implementation):
        if owner == "thread":
            return await asyncio.to_thread(_run, case, control, implementation)

        async def child():
            return _run(case, control, implementation)

        return await asyncio.create_task(child())

    expected = await invoke(_frozen())
    trace, bodies = tuple(probe.trace), dict(case.blobs)
    probe.trace.clear()
    assert await invoke(module._observe_final_versions) == expected
    assert tuple(probe.trace) == trace and case.blobs == bodies
    assert all(mode != "local" for mode, _ in trace)


def _error(kind):
    if kind == "cancel":
        return TurnCancelled("original cancel")
    if kind == "task":
        return asyncio.CancelledError("original task cancel")
    if kind == "timeout":
        return TimeoutError("original deadline")
    if kind == "os":
        return OSError("original upstream failure")
    if kind == "kernel":
        return KernelError("workspace_closure_corrupt", "original control, not data")
    if kind == "marked":
        return UpstreamCheckpointError(OSError("original mark"))
    return UpstreamCheckpointError(UpstreamCheckpointError(OSError("original nested mark")))


@pytest.mark.parametrize("kind", ["cancel", "task", "timeout", "os", "kernel", "marked", "nested"])
@pytest.mark.parametrize(
    "failure",
    [
        ("full", "capture-entry"),
        ("local", "native"),
        ("full", "capture-exit"),
        ("local", "encode"),
        ("full", "encoded"),
    ],
)
def test_capture_control_first_error_is_original_and_never_reaches_cas(capture_case, kind, failure):
    case, probe = capture_case, capture_case.probe
    error = _error(kind)
    probe.failure, probe.error = failure, error
    before = dict(case.blobs)
    with pytest.raises(BaseException) as caught:
        _run(case, probe.control)
    assert caught.value is error and case.blobs == before
    assert probe.trace[-1] == failure
    assert not any(mode in {"write", "read"} for mode, _ in probe.trace)


@pytest.mark.parametrize("changed", ["owner", "origin"])
def test_native_drift_is_detected_at_exit_before_encoding_or_cas(
    capture_case, monkeypatch, changed
):
    case, probe = capture_case, capture_case.probe
    binding, frozen = object(), object()
    current = frozen
    error = KernelError("git_source_owner_changed", "original owner drift")

    def full():
        probe.full()
        if current is not frozen:
            raise error

    def local():
        nonlocal current
        probe.local()
        if probe.phase == "native":
            if changed == "owner":
                current = binding
            else:
                control._authenticate = lambda: None

    control = GitAuthenticationControl(local, full)
    with pytest.raises(KernelError) as caught:
        _run(case, control)
    if changed == "owner":
        assert caught.value is error and probe.trace[-1] == ("full", "capture-exit")
    else:
        assert caught.value.code == "git_authentication_control_invalid"
    assert not any(mode in {"write", "read"} or phase == "encode" for mode, phase in probe.trace)
