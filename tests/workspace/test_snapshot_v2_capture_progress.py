"""捕获进度端口：真实原生观察、原 codec、耐久 CAS 与固定旧实现负控。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    same_task_io_git_authentication,
    same_task_pure_git_authentication,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace import parent_closure_codec as codec
from harnessix.workspace import snapshot as native
from harnessix.workspace import snapshot_v2 as module
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="实际 POSIX 原生捕获")

_ORACLE_COMMIT = "80c4184f3d20e0d7e6741dbe2ab7c701ee5fb710"
_ORACLE_SOURCE_SHA256 = "42bd9d9e71da70ffda7253c1b7128767eebbdc32e3bd3c7e32892a8beab162e0"
_ORACLE_PATH = Path(__file__).with_name("fixtures") / "snapshot_v2_capture_before.py"


@pytest.fixture(scope="module")
def frozen():
    raw = _ORACLE_PATH.read_bytes()
    commit, source_path, source_hash, unchanged, source = raw.split(b"\n", 4)
    assert commit == f"# Frozen Git commit: {_ORACLE_COMMIT}".encode()
    assert source_path == b"# Frozen Git path: src/harnessix/workspace/snapshot_v2.py"
    assert unchanged == b"# The source below is byte-for-byte unchanged."
    assert source_hash == f"# Frozen source SHA256: {_ORACLE_SOURCE_SHA256}".encode()
    assert hashlib.sha256(source).hexdigest() == _ORACLE_SOURCE_SHA256
    spec = importlib.util.spec_from_file_location("snapshot_v2_capture_before", _ORACLE_PATH)
    assert spec is not None and spec.loader is not None
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    return oracle


@dataclass(frozen=True)
class _Case:
    root: Path
    requests: tuple[WorkspaceResourceRequest, ...]
    cwd: str = "."
    external: dict | None = None
    parents: frozenset[tuple[str, str]] = frozenset()

    def arguments(self):
        return {"resources": self.requests, "cwd": self.cwd, "external_roots": self.external}


@pytest.fixture
def case(tmp_path):
    root, external = tmp_path / "root", tmp_path / "external"
    for base, path, body in (
        (root, "资料/子目录/报告.txt", "正文\n".encode()),
        (root, "left/deep/alpha.bin", b"\x00native\xff\n"),
        (external, "data/β.txt", b"external\n"),
    ):
        leaf = base / path
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(body)
    (root / "empty").mkdir()
    requests = (
        WorkspaceResourceRequest(path="资料/子目录/报告.txt", access="read"),
        WorkspaceResourceRequest(path="left/deep/alpha.bin", access="write"),
        WorkspaceResourceRequest(path="left/deep/pending.bin", access="write"),
        WorkspaceResourceRequest(path="empty", access="read"),
        WorkspaceResourceRequest(location="cache", path="data/β.txt", access="read"),
    )
    return _Case(
        root,
        requests,
        cwd="资料/子目录",
        external={"cache": (external, ("read",))},
        parents=frozenset(
            {
                ("cache", "."),
                ("cache", "data"),
                ("workspace", "."),
                ("workspace", "left"),
                ("workspace", "left/deep"),
                ("workspace", "资料"),
                ("workspace", "资料/子目录"),
            }
        ),
    )


class _Progress:
    def __init__(self, failure=None, error=None, failure_occurrence=1):
        self.active = None
        self.events = []
        self.counts = {"native": 0, "pure": 0}
        self.saved = []
        self.failure, self.error = failure, error
        self.failure_occurrence = failure_occurrence
        self.failure_seen = 0
        self.in_native_io = False
        self.blobs = ()
        self.facts = None
        # 保存同一个端口对象，断言真实委托收到的不是替代 callback。
        self.full = self._full

    def record(self, *event):
        self.events.append(event)
        if event == self.failure:
            self.failure_seen += 1
            if self.failure_seen == self.failure_occurrence:
                raise self.error

    def _full(self):
        assert self.active is None, "完整 CAS/控制不能借用 native 或 pure 段"
        self.record("full")

    @contextmanager
    def segment(self, kind):
        assert self.active is None, "native 与 pure 段不能重叠"
        self.counts[kind] += 1
        index = self.counts[kind]
        self.record(f"{kind}.enter", index)
        self.active = kind, index

        def check():
            assert self.active == (kind, index)
            self.record(f"{kind}.check", index)
            if kind == "native" and self.in_native_io:
                self.record("native.io")

        self.saved.append(check)
        try:
            yield check
        finally:
            self.active = None
            self.record("cleanup", kind, index)
        self.record(f"{kind}.exit", index)

    def pure(self):
        self.record("pure.factory", self.counts["pure"] + 1)
        return self.segment("pure")

    def options(self, native_port=True, pure_port=True):
        return {
            "native_progress": self.segment("native") if native_port else None,
            "pure_progress": self.pure if pure_port else None,
        }


def _observe_real_calls(monkeypatch, api, progress, *, native_port=False, pure_port=False):
    original_capture = api.capture_snapshot_facts
    original_encode = api._encode_snapshot
    original_verified = api.read_verified_body
    original_history = api.read_workspace_parent_closure
    original_observe = native._PosixRoot.observe
    original_check = NativeReadOperation.checkpoint

    def capture(root, **kwargs):
        assert progress.active == (("native", 1) if native_port else None)
        assert kwargs["checkpoint"] is (progress.saved[-1] if native_port else progress.full)
        progress.record("capture.start")
        facts = original_capture(root, **kwargs)
        progress.facts = facts
        progress.record("capture.end")
        return facts

    def encode(facts, checkpoint):
        assert progress.active == (("pure", 1) if pure_port else None)
        assert checkpoint is (progress.saved[-1] if pure_port else progress.full)
        progress.record("encode.start")
        result = original_encode(facts, checkpoint)
        progress.blobs = result[1]
        progress.record("encode.end")
        return result

    def verified(digest, size, read_blob, checkpoint):
        assert progress.active is None and checkpoint is progress.full
        progress.record("verified", digest, size)
        return original_verified(digest, size, read_blob, checkpoint)

    def history(snapshot, read_blob, *, checkpoint, **kwargs):
        assert progress.active is None and checkpoint is progress.full
        assert kwargs.get("pure_progress") is not None if pure_port else not kwargs
        progress.record("history.start")
        parents = original_history(snapshot, read_blob, checkpoint=checkpoint, **kwargs)
        progress.record("history.end")
        return parents

    def observe(root, path, *, access, checkpoint=None):
        progress.record("native.observe", path, access)
        return original_observe(root, path, access=access, checkpoint=checkpoint)

    def native_check(operation):
        progress.in_native_io = True
        try:
            return original_check(operation)
        finally:
            progress.in_native_io = False

    monkeypatch.setattr(api, "capture_snapshot_facts", capture)
    monkeypatch.setattr(api, "_encode_snapshot", encode)
    monkeypatch.setattr(api, "read_verified_body", verified)
    monkeypatch.setattr(api, "read_workspace_parent_closure", history)
    monkeypatch.setattr(native._PosixRoot, "observe", observe)
    monkeypatch.setattr(NativeReadOperation, "checkpoint", native_check)


def _store_ports(monkeypatch, store, progress):
    original_read = store._read_blob

    def physical_read(digest):
        assert progress.active is None
        progress.record("physical.read", digest)
        return original_read(digest)

    def write(digest, body):
        assert progress.active is None
        progress.record("write", digest, body)
        store.put_blob(digest, body)
        progress.record("durable", digest)

    def read(digest):
        assert progress.active is None
        progress.record("read", digest)
        return store._read_blob(digest)

    monkeypatch.setattr(store, "_read_blob", physical_read)
    return write, read


def _capture(api, case, progress, write, read, **options):
    return api.capture_workspace_snapshot_v2(
        case.root,
        checkpoint=progress.full,
        write_blob=write,
        read_blob=read,
        **case.arguments(),
        **options,
    )


def _cas_events(events):
    return tuple(event for event in events if event[0] in {"write", "read", "physical.read"})


@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_and_explicit_none_match_pinned_full_trace_and_wire(
    case, tmp_path, monkeypatch, frozen, explicit_none
):
    results = []
    for label, api, options in (
        ("before", frozen, {}),
        (
            "current",
            module,
            {"native_progress": None, "pure_progress": None} if explicit_none else {},
        ),
    ):
        progress = _Progress()
        with monkeypatch.context() as patch:
            _observe_real_calls(patch, api, progress)
            with SQLiteWorkspaceTransactionStore(
                tmp_path / label, checkpoint=progress.full
            ) as store:
                write, read = _store_ports(patch, store, progress)
                snapshot = _capture(api, case, progress, write, read, **options)
                bodies = {path.name: path.read_bytes() for path in store._blobs.iterdir()}
                results.append((snapshot.model_dump_json().encode(), progress.events, bodies))
    assert results[0] == results[1]
    assert any(event[0] == "native.observe" for event in results[0][1])
    assert any(event[0] == "encode.start" for event in results[0][1])
    assert any(event[0] == "physical.read" for event in results[0][1])


@pytest.mark.parametrize("native_port,pure_port", [(True, False), (False, True), (True, True)])
def test_disjoint_real_segments_keep_all_cas_full_order_and_complete_closure(
    case, tmp_path, monkeypatch, frozen, native_port, pure_port
):
    old_progress = _Progress()
    with monkeypatch.context() as patch:
        _observe_real_calls(patch, frozen, old_progress)
        with SQLiteWorkspaceTransactionStore(
            tmp_path / "baseline", checkpoint=old_progress.full
        ) as baseline:
            old_write, old_read = _store_ports(patch, baseline, old_progress)
            expected = _capture(frozen, case, old_progress, old_write, old_read)
            expected_bodies = {path.name: path.read_bytes() for path in baseline._blobs.iterdir()}
    progress = _Progress()
    _observe_real_calls(monkeypatch, module, progress, native_port=native_port, pure_port=pure_port)
    state = tmp_path / "current"
    with SQLiteWorkspaceTransactionStore(state, checkpoint=progress.full) as store:
        write, read = _store_ports(monkeypatch, store, progress)
        actual = _capture(
            module, case, progress, write, read, **progress.options(native_port, pure_port)
        )
        bodies = {path.name: path.read_bytes() for path in store._blobs.iterdir()}
    assert actual.model_dump_json().encode() == expected.model_dump_json().encode()
    assert bodies == expected_bodies == dict(progress.blobs)
    assert _cas_events(progress.events) == _cas_events(old_progress.events)
    digests = [digest for digest, _ in progress.blobs]
    callbacks = [(event[0], event[1]) for event in progress.events if event[0] in {"write", "read"}]
    assert callbacks == [
        *(event for digest in digests for event in (("write", digest), ("read", digest))),
        ("read", digests[-1]),
        *(("read", digest) for digest in digests[:-1]),
    ]
    for index, event in enumerate(progress.events):
        if event[0] in {"write", "read"}:
            assert progress.events[index - 1] == ("full",)
    assert progress.events[-1] == ("full",)
    assert progress.counts == {"native": int(native_port), "pure": 3 if pure_port else 0}
    if native_port:
        assert progress.events.index(("native.exit", 1)) < progress.events.index(("encode.start",))
    if pure_port:
        assert progress.events.index(("capture.end",)) < progress.events.index(("pure.enter", 1))
        assert progress.events.index(("pure.exit", 1)) < next(
            index for index, event in enumerate(progress.events) if event[0] == "write"
        )
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as reopened:
        parents = codec.read_workspace_parent_closure(
            actual, reopened._read_blob, checkpoint=lambda: None
        )
        assert parents == progress.facts.parents
        assert {(item.location, item.path) for item in parents} == case.parents
        assert reopened._db.total_changes == 0
        assert {path.name: path.read_bytes() for path in reopened._blobs.iterdir()} == bodies
    assert (
        next(item for item in actual.resources if item.path.endswith("pending.bin")).kind
        == "missing"
    )
    assert (case.root / "资料/子目录/报告.txt").read_bytes() == "正文\n".encode()


def _spread(root, count, depth):
    root.mkdir()
    requests, parents = [], {("workspace", ".")}
    for index in range(count):
        # 128 段仍是真实深链；短 Unicode 组件不与 macOS 的宿主 PATH_MAX 混淆。
        parts = [f"支{index:03}", *("层" for _ in range(depth - 1))]
        directory = root.joinpath(*parts)
        directory.mkdir(parents=True)
        (directory / "正文.txt").write_bytes(b"native\n")
        requests.append(
            WorkspaceResourceRequest(path="/".join((*parts, "正文.txt")), access="read")
        )
        parents.update(("workspace", "/".join(parts[:level])) for level in range(1, depth + 1))
    return _Case(root, tuple(requests), parents=frozenset(parents))


@pytest.mark.parametrize(
    "count,depth,explicit_cwd", [(8, 3, False), (255, 4, False), (255, 4, True), (1, 127, False)]
)
def test_real_posix_unicode_capacity_depth_and_durable_readonly_reopen(
    tmp_path, count, depth, explicit_cwd
):
    case = _spread(tmp_path / "root", count, depth)
    arguments = case.arguments()
    if explicit_cwd:
        arguments["resources"] = (*case.requests, WorkspaceResourceRequest(path=".", access="read"))
    progress = _Progress()
    state = tmp_path / "cas"
    with SQLiteWorkspaceTransactionStore(state, checkpoint=progress.full) as store:
        snapshot = module.capture_workspace_snapshot_v2(
            case.root,
            checkpoint=progress.full,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
            **arguments,
            **progress.options(),
        )
        bodies = {path.name: path.read_bytes() for path in store._blobs.iterdir()}
    assert len(snapshot.resources) == count + 1
    assert snapshot.parent_closure.parent_count == count * depth + 1
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as reopened:
        parents = codec.read_workspace_parent_closure(
            snapshot, reopened._read_blob, checkpoint=lambda: None
        )
        assert {(item.location, item.path) for item in parents} == case.parents
        assert (
            module.verify_workspace_snapshot_v2(
                snapshot, case.root, checkpoint=lambda: None, read_blob=reopened._read_blob
            )
            == snapshot
        )
        assert reopened._db.total_changes == 0
        assert bodies == {path.name: path.read_bytes() for path in reopened._blobs.iterdir()}
    assert all((case.root / request.path).read_bytes() == b"native\n" for request in case.requests)


def test_multiple_real_codec_chunks_are_confirmed_before_manifest_and_fully_reread(
    tmp_path, monkeypatch
):
    # 缩小测试分块阈值，仍执行真实编码/合同校验；不伪造 codec 的成功返回。
    monkeypatch.setattr(codec, "MAX_CLOSURE_BLOB_BYTES", 8192)
    case = _spread(tmp_path / "root", 32, 3)
    progress = _Progress()
    _observe_real_calls(monkeypatch, module, progress, native_port=True, pure_port=True)
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas", checkpoint=progress.full) as store:
        write, read = _store_ports(monkeypatch, store, progress)
        snapshot = _capture(module, case, progress, write, read, **progress.options())
        manifest = json.loads(store._read_blob(snapshot.parent_closure.sha256))
        chunks = [item["sha256"] for item in manifest["chunks"]]
        assert len(chunks) > 1
        assert [digest for digest, _ in progress.blobs] == [*chunks, snapshot.parent_closure.sha256]
        reads = [event[1] for event in progress.events if event[0] == "read"]
        assert reads == [
            *chunks,
            snapshot.parent_closure.sha256,
            snapshot.parent_closure.sha256,
            *chunks,
        ]
        assert progress.counts == {"native": 1, "pure": len(chunks) + 2}
        assert {(item.location, item.path) for item in progress.facts.parents} == case.parents


_ERROR_NAMES = [
    "cancel",
    "task",
    "deadline",
    "os",
    "kernel-workspace",
    "kernel-delivery",
    "value",
    "marked",
    "nested",
]


def _error(name):
    if name == "cancel":
        return TurnCancelled("original cancellation")
    if name == "task":
        return asyncio.CancelledError("original task cancellation")
    if name == "deadline":
        return KernelError("workspace_timeout", "original deadline")
    if name.startswith("kernel-"):
        code = (
            "workspace_closure_corrupt" if name == "kernel-workspace" else "delivery_blob_corrupt"
        )
        return KernelError(code, "control, not corrupt data")
    error = OSError("original upstream error")
    if name == "marked":
        return UpstreamCheckpointError(error)
    if name == "nested":
        return UpstreamCheckpointError(UpstreamCheckpointError(error))
    return ValueError("original control") if name == "value" else error


_FAILURE_PHASES = [
    ("native.enter", 1),
    ("native.check", 1),
    ("native.io",),
    ("native.exit", 1),
    ("full",),
    *(
        (f"pure.{phase}", segment)
        for segment in (1, 2, 3)
        for phase in ("factory", "enter", "check", "exit")
    ),
]


@pytest.mark.parametrize("name", _ERROR_NAMES)
@pytest.mark.parametrize("phase", _FAILURE_PHASES, ids=lambda phase: "-".join(map(str, phase)))
def test_each_port_boundary_preserves_first_error_identity_and_stops_work(
    case, tmp_path, monkeypatch, phase, name
):
    error = _error(name)
    progress = _Progress(phase, error)
    _observe_real_calls(monkeypatch, module, progress, native_port=True, pure_port=True)
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas", checkpoint=progress.full) as store:
        write, read = _store_ports(monkeypatch, store, progress)
        with pytest.raises(BaseException) as caught:
            _capture(module, case, progress, write, read, **progress.options())
    assert caught.value is error
    assert progress.active is None
    failure_index = progress.events.index(phase)
    assert all(event[0] == "cleanup" for event in progress.events[failure_index + 1 :])
    if phase[0].startswith("native") or (phase[0].startswith("pure") and phase[1] == 1):
        assert not _cas_events(progress.events)
    if phase[0].startswith("native"):
        assert ("encode.start",) not in progress.events


@pytest.mark.parametrize("name", _ERROR_NAMES)
def test_every_original_full_checkpoint_stops_at_exact_first_error(
    case, tmp_path, monkeypatch, name
):
    def run(label, progress):
        with monkeypatch.context() as patch:
            _observe_real_calls(patch, module, progress, native_port=True, pure_port=True)
            with SQLiteWorkspaceTransactionStore(
                tmp_path / label, checkpoint=progress.full
            ) as store:
                write, read = _store_ports(patch, store, progress)
                return _capture(module, case, progress, write, read, **progress.options())

    baseline = _Progress()
    run("baseline", baseline)
    boundaries = [index for index, event in enumerate(baseline.events) if event == ("full",)]
    assert boundaries and baseline.events[boundaries[-1]] == baseline.events[-1]
    for occurrence, index in enumerate(boundaries, start=1):
        error = _error(name)
        progress = _Progress(("full",), error, failure_occurrence=occurrence)
        with pytest.raises(BaseException) as caught:
            run(f"full-{occurrence}", progress)
        assert caught.value is error, f"full checkpoint {occurrence} changed the first error"
        assert progress.events == baseline.events[: index + 1], (
            f"full checkpoint {occurrence} continued"
        )
        assert progress.active is None


@pytest.mark.parametrize("segment", ["native", "encode", "history-chunk", "history-digest"])
@pytest.mark.parametrize("name", _ERROR_NAMES)
def test_real_same_task_control_does_not_authenticate_exit_over_first_local_failure(
    case, tmp_path, monkeypatch, segment, name
):
    error, exit_error = _error(name), KernelError("git_source_owner_changed", "later exit")
    active, failed, pure_count = None, False, 0
    saved, full_calls, local_calls = [], [], []
    in_native_io = False
    original = NativeReadOperation.checkpoint

    def native_check(operation):
        nonlocal in_native_io
        in_native_io = True
        try:
            return original(operation)
        finally:
            in_native_io = False

    monkeypatch.setattr(NativeReadOperation, "checkpoint", native_check)

    def local():
        nonlocal failed
        local_calls.append(active)
        if active == segment and (segment != "native" or in_native_io):
            failed = True
            raise error

    def full():
        full_calls.append(active)
        if failed:
            raise exit_error

    control = GitAuthenticationControl(local, full)

    @contextmanager
    def native_progress():
        nonlocal active
        with same_task_io_git_authentication(control) as check:
            active = "native"
            saved.append(check)
            try:
                yield check
            finally:
                active = None

    @contextmanager
    def pure_progress():
        nonlocal active, pure_count
        pure_count += 1
        with same_task_pure_git_authentication(control) as check:
            active = ("encode", "history-chunk", "history-digest")[pure_count - 1]
            saved.append(check)
            try:
                yield check
            finally:
                active = None

    with SQLiteWorkspaceTransactionStore(tmp_path / "cas", checkpoint=control) as store:
        with pytest.raises(BaseException) as caught:
            module.capture_workspace_snapshot_v2(
                case.root,
                checkpoint=control,
                write_blob=store.put_blob,
                read_blob=store._read_blob,
                **case.arguments(),
                native_progress=native_progress(),
                pure_progress=pure_progress,
            )
    assert caught.value is error and failed and active is None
    assert segment in local_calls
    # 首失败之后完整出口若被执行就会抛另一对象；保存端口随后必须恢复完整检查。
    before = len(full_calls), len(local_calls)
    with pytest.raises(KernelError) as revoked:
        saved[-1]()
    assert revoked.value is exit_error
    assert (len(full_calls), len(local_calls)) == (before[0] + 1, before[1])


@pytest.mark.parametrize("failure", ["stopped", "deadline"])
def test_native_operation_keeps_original_stop_and_deadline_before_encode_or_cas(
    case, tmp_path, monkeypatch, failure
):
    original = NativeReadOperation.__init__
    operations = []

    def initialize(operation, checkpoint):
        original(operation, checkpoint)
        operations.append(operation)
        if failure == "stopped":
            operation.stopped.set()
        else:
            operation.deadline = time.monotonic() - 1

    monkeypatch.setattr(NativeReadOperation, "__init__", initialize)
    progress = _Progress()
    _observe_real_calls(monkeypatch, module, progress, native_port=True, pure_port=True)
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas", checkpoint=progress.full) as store:
        write, read = _store_ports(monkeypatch, store, progress)
        with pytest.raises(TurnCancelled if failure == "stopped" else KernelError) as caught:
            _capture(module, case, progress, write, read, **progress.options())
    assert operations and progress.active is None
    assert not _cas_events(progress.events) and ("encode.start",) not in progress.events
    if failure == "deadline":
        assert caught.value.code == "workspace_timeout"


@pytest.mark.parametrize(
    "boundary",
    [
        "native-exit",
        "encode-exit",
        "write",
        "immediate-read",
        "historical-manifest",
        "historical-pure",
    ],
)
def test_owner_drift_is_rejected_at_next_full_boundary_without_next_cas(case, tmp_path, boundary):
    changed, pure_count = False, 0
    writes, reads, saved = [], [], []
    error = KernelError("git_source_owner_changed", boundary)

    def full():
        if changed:
            raise error

    control = GitAuthenticationControl(lambda: None, full)

    @contextmanager
    def native_progress():
        nonlocal changed
        with same_task_io_git_authentication(control) as check:
            saved.append(check)
            yield check
            if boundary == "native-exit":
                changed = True

    @contextmanager
    def pure_progress():
        nonlocal changed, pure_count
        pure_count += 1
        with same_task_pure_git_authentication(control) as check:
            saved.append(check)
            yield check
            if (boundary == "encode-exit" and pure_count == 1) or (
                boundary == "historical-pure" and pure_count == 2
            ):
                changed = True

    with SQLiteWorkspaceTransactionStore(tmp_path / "cas", checkpoint=control) as store:

        def write(digest, body):
            nonlocal changed
            writes.append(digest)
            store.put_blob(digest, body)
            if boundary == "write":
                changed = True

        def read(digest):
            nonlocal changed
            reads.append(digest)
            body = store._read_blob(digest)
            if (boundary == "immediate-read" and len(reads) == 1) or (
                boundary == "historical-manifest" and len(reads) == 3
            ):
                changed = True
            return body

        with pytest.raises(KernelError) as caught:
            module.capture_workspace_snapshot_v2(
                case.root,
                checkpoint=control,
                write_blob=write,
                read_blob=read,
                **case.arguments(),
                native_progress=native_progress(),
                pure_progress=pure_progress,
            )
    assert caught.value is error and changed
    assert (len(writes), len(reads)) == {
        "native-exit": (0, 0),
        "encode-exit": (0, 0),
        "write": (1, 0),
        "immediate-read": (1, 1),
        "historical-manifest": (2, 3),
        "historical-pure": (2, 4),
    }[boundary]
    with pytest.raises(KernelError) as revoked:
        saved[-1]()
    assert revoked.value is error


@pytest.mark.parametrize(
    "read_index",
    [1, 2, 3, 4],
    ids=["immediate-chunk", "immediate-manifest", "historical-manifest", "historical-chunk"],
)
@pytest.mark.parametrize("damage", ["changed-bytes", "missing"])
def test_real_bad_cas_never_returns_snapshot_or_continues_after_corruption(
    case, tmp_path, monkeypatch, read_index, damage
):
    progress = _Progress()
    _observe_real_calls(monkeypatch, module, progress, native_port=True, pure_port=True)
    reads, writes = [], []
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas", checkpoint=progress.full) as store:
        write_port, read_port = _store_ports(monkeypatch, store, progress)

        def write(digest, body):
            writes.append(digest)
            write_port(digest, body)

        def read(digest):
            reads.append(digest)
            if len(reads) == read_index:
                path = store._blobs / digest
                if damage == "missing":
                    path.unlink()
                else:
                    body = path.read_bytes()
                    path.write_bytes(b"!" + body[1:])
            return read_port(digest)

        with pytest.raises(KernelError) as caught:
            _capture(module, case, progress, write, read, **progress.options())
    assert caught.value.code == "workspace_closure_corrupt"
    assert len(reads) == read_index and len(writes) == min(read_index, 2)
    assert progress.counts == {"native": 1, "pure": 1}
    assert progress.active is None and ("history.end",) not in progress.events


@pytest.mark.parametrize("foreign", ["thread", "task"])
def test_foreign_task_or_thread_does_not_inherit_local_control_or_change_frozen_trace(
    case, tmp_path, monkeypatch, frozen, foreign
):
    progress = _Progress()
    local_calls = []
    control = GitAuthenticationControl(lambda: local_calls.append(1), progress.full)
    results = []

    def run(api, label, layered):
        progress.events.clear()
        progress.saved.clear()
        progress.counts = {"native": 0, "pure": 0}
        options = (
            {
                "native_progress": same_task_io_git_authentication(control),
                "pure_progress": lambda: same_task_pure_git_authentication(control),
            }
            if layered
            else {}
        )
        with monkeypatch.context() as patch:
            # foreign adapter 委托原 control；这里只旁观，不替换控制或成功返回。
            original_observe = native._PosixRoot.observe
            original_encode = api._encode_snapshot

            def observe(root, path, *, access, checkpoint=None):
                progress.record("native.observe", path, access)
                return original_observe(root, path, access=access, checkpoint=checkpoint)

            def encode(facts, checkpoint):
                assert checkpoint is control
                progress.record("encode.start")
                result = original_encode(facts, checkpoint)
                progress.record("encode.end")
                return result

            patch.setattr(native._PosixRoot, "observe", observe)
            patch.setattr(api, "_encode_snapshot", encode)
            with SQLiteWorkspaceTransactionStore(tmp_path / label, checkpoint=control) as store:
                write, read = _store_ports(patch, store, progress)
                result = api.capture_workspace_snapshot_v2(
                    case.root,
                    checkpoint=control,
                    write_blob=write,
                    read_blob=read,
                    **case.arguments(),
                    **options,
                )
                return result.model_dump_json().encode(), tuple(progress.events)

    def pair():
        results.append(run(frozen, "old", False))
        results.append(run(module, "new", True))

    if foreign == "thread":
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(pair).result()
    else:

        async def owner_task():
            nonlocal control
            control = GitAuthenticationControl(lambda: local_calls.append(1), progress.full)

            async def other():
                pair()

            await asyncio.create_task(other())

        asyncio.run(owner_task())
    assert results[0] == results[1]
    assert not local_calls


@pytest.mark.parametrize("failure", ["resources", "path-depth", "external-grant", "symlink"])
def test_existing_admission_and_native_denial_do_not_encode_or_write_cas(
    case, tmp_path, monkeypatch, failure
):
    arguments = case.arguments()
    if failure == "resources":
        arguments["resources"] = tuple(
            WorkspaceResourceRequest(path=f"leaf{i:03}", access="read") for i in range(256)
        )
    elif failure == "path-depth":
        arguments["resources"] = (
            WorkspaceResourceRequest(path="/".join(["p"] * 129), access="read"),
        )
    elif failure == "external-grant":
        arguments["external_roots"] = {"cache": (case.external["cache"][0], ("write",))}
    else:
        leaf = case.root / "资料/子目录/报告.txt"
        leaf.unlink()
        leaf.symlink_to(case.root / "left/deep/alpha.bin")
    progress = _Progress()
    _observe_real_calls(monkeypatch, module, progress, native_port=True, pure_port=True)
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas", checkpoint=progress.full) as store:
        write, read = _store_ports(monkeypatch, store, progress)
        with pytest.raises(KernelError) as caught:
            module.capture_workspace_snapshot_v2(
                case.root,
                checkpoint=progress.full,
                write_blob=write,
                read_blob=read,
                **arguments,
                **progress.options(),
            )
        assert not tuple(store._blobs.iterdir())
    assert (
        caught.value.code
        == {
            "resources": "workspace_snapshot_limit",
            "path-depth": "workspace_path_denied",
            "external-grant": "workspace_access_denied",
            "symlink": "workspace_path_denied",
        }[failure]
    )
    assert not _cas_events(progress.events) and ("encode.start",) not in progress.events
