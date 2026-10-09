"""同步 Source 文件端口的有界机制矩阵；不代表 SDK/Owner 权限或负载验收。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import planner
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import git_delivery_source as module
from harnessix.product_config.git_native_control import protected_git_control
from harnessix.tools.contracts import ReadToolError
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 路径/FD/分块读取")

_COMMIT = "e13db8c5f5b20efb7a5911f723e831891c60e444"
_SOURCE_SHA256 = "dbf0ef13406872ac266dd6eb684c9ac601e44a27b5d02dd01382f2b6ac07a722"
_FIXTURE_SHA256 = "4fbf642b0f79de58fd3b96a144d859bedbfb56ce51aa1ab6e61b7a03bd86d781"
_ERROR_NAMES = ("cancel", "task", "timeout", "os", "kernel", "marked", "nested")


def _frozen():
    path = Path(__file__).with_name("fixtures") / "git_source_file_before.txt"
    body = path.read_bytes()
    assert hashlib.sha256(body).hexdigest() == _FIXTURE_SHA256
    assert f"# Frozen Git commit: {_COMMIT}".encode() in body
    assert f"# Frozen source SHA256: {_SOURCE_SHA256}".encode() in body
    namespace = dict(vars(module))
    # 生产已移除旧实现的导入，固定 AST 仍必须使用其原正式依赖。
    namespace["protected_git_control"] = protected_git_control
    exec(compile(body, str(path), "exec"), namespace)
    return namespace["_read_source_file"]


def _error(name):
    if name == "kernel":
        return KernelError("delivery_source_changed", "original control, not IO failure")
    if name == "marked":
        return UpstreamCheckpointError(OSError("original mark"))
    if name == "nested":
        return UpstreamCheckpointError(UpstreamCheckpointError(OSError("original nested mark")))
    return {
        "cancel": TurnCancelled,
        "task": asyncio.CancelledError,
        "timeout": TimeoutError,
        "os": OSError,
    }[name]("original control")


@dataclass(frozen=True)
class _File:
    root: Path
    path: str
    body: bytes
    mode: int

    @property
    def leaf(self):
        return self.root / self.path


def _make_file(root, depth=25, size=2 * 65536 + 137, mode=0o644):
    parts = [f"d{level:02}" for level in range(depth)]
    path = "/".join((*parts, "正文.bin"))
    leaf = root / path
    leaf.parent.mkdir(parents=True)
    body = (bytes(range(256)) * ((size + 255) // 256))[:size]
    leaf.write_bytes(body)
    leaf.chmod(mode)
    return _File(root, path, body, mode)


@pytest.fixture
def file(tmp_path):
    return _make_file(tmp_path / "root")


class _Probe:
    """只读机制资源探针：内存 Owner/锁代际及真实根身份，不是授权证明。"""

    def __init__(self, file):
        self.file = file
        info = file.root.stat()
        self.root_identity = info.st_dev, info.st_ino
        self.owner = self.original_owner = object()
        self.resource = self.original_resource = object()
        self.owner_error = KernelError("git_source_owner_changed", "original owner drift")
        self.root_error = KernelError("git_source_owner_changed", "original root drift")
        self.resource_error = KernelError("git_source_owner_changed", "original locked resource")
        self.cancel = CancelToken()
        self.deadline = time.monotonic() + 60
        self.clock = time.monotonic
        self.phase = "outside"
        self.active_io = False
        self.data_fd = None
        self.native_fds = set()
        self.events = []
        self.saved = None
        self.failure = None
        self.error = None
        self.local_hook = None
        self.after_block = None
        self.after_existing = None
        self.chunks = 0
        self.bind()

    def bind(self):
        self.control = GitAuthenticationControl(self.local, self.full)
        return self.control

    def record(self, mode, detail=None):
        self.events.append((mode, self.phase, detail))
        if self.failure == (mode, self.phase):
            raise self.error

    def local(self):
        self.record("local")
        if self.local_hook is not None:
            self.local_hook()
        self.cancel.checkpoint()
        if self.clock() >= self.deadline:
            raise TimeoutError("same original deadline expired")
        if self.resource is not self.original_resource:
            raise self.resource_error

    def full(self):
        self.record("full")
        if self.owner is not self.original_owner:
            raise self.owner_error
        info = self.file.root.stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise self.root_error


def _observe_real_reader(monkeypatch, probe):
    original_existing = module._read_existing
    original_all = planner._read_all
    original_open, original_stat, original_read = os.open, os.stat, os.read

    def existing(root, path, platform, *, checkpoint=None):
        probe.saved = checkpoint
        probe.phase, probe.active_io = "path", True
        try:
            result = original_existing(root, path, platform, checkpoint=checkpoint)
        finally:
            probe.phase, probe.active_io = "after-existing", False
            assert not probe.native_fds, "原生读取成功或失败后都必须关闭本次打开的 FD"
        if probe.after_existing is not None:
            probe.after_existing()
        return result

    def read_all(descriptor, *, checkpoint=None):
        probe.phase, probe.data_fd = "block", descriptor
        try:
            return original_all(descriptor, checkpoint=checkpoint)
        finally:
            probe.phase, probe.data_fd = "path", None

    def open_(path, flags, *args, **kwargs):
        if probe.active_io:
            probe.record("open", (str(path), flags))
        descriptor = original_open(path, flags, *args, **kwargs)
        if probe.active_io:
            probe.native_fds.add(descriptor)
        return descriptor

    original_close = os.close

    def close_(descriptor):
        original_close(descriptor)
        probe.native_fds.discard(descriptor)

    def stat_(path, *args, **kwargs):
        if probe.active_io:
            probe.record("stat", (str(path), kwargs.get("follow_symlinks", True)))
        return original_stat(path, *args, **kwargs)

    def read_(descriptor, size):
        if descriptor != probe.data_fd or not probe.active_io:
            return original_read(descriptor, size)
        probe.record("read", size)
        body = original_read(descriptor, size)
        probe.record("chunk", len(body))
        probe.chunks += 1
        if probe.after_block is not None and probe.chunks == 1:
            probe.after_block()
        return body

    monkeypatch.setattr(module, "_read_existing", existing)
    monkeypatch.setattr(planner, "_read_all", read_all)
    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "stat", stat_)
    monkeypatch.setattr(os, "read", read_)
    monkeypatch.setattr(os, "close", close_)


def _read(file, probe, *, implementation=module._read_source_file, callback=None):
    return implementation(file.root, file.path, "posix", callback or probe.control)


def _controls(events):
    return [(mode, phase) for mode, phase, _ in events if mode in {"full", "local"}]


def _callback(probe, kind):
    class Proxy:
        def __call__(self):
            probe.full()

    class Subclass(GitAuthenticationControl):
        pass

    if kind == "function":
        return probe.full
    if kind == "proxy":
        return Proxy()
    return Subclass(probe.local, probe.full)


@pytest.mark.parametrize(
    "depth,size,mode", [(1, 0, 0o644), (25, 65537, 0o755), (25, 131209, 0o644)]
)
def test_exact_file_keeps_real_fd_chain_chunk_bytes_and_first_last_full(
    tmp_path, monkeypatch, depth, size, mode
):
    file = _make_file(tmp_path / "root", depth, size, mode)
    probe = _Probe(file)
    origin = probe.control._origin
    _observe_real_reader(monkeypatch, probe)
    assert _read(file, probe) == (file.body, mode)
    controls = _controls(probe.events)
    assert controls[0] == ("full", "outside")
    assert controls[-1] == ("full", "after-existing")
    assert sum(mode == "full" for mode, _ in controls) == 2
    assert {"path", "block"} <= {phase for mode, phase in controls if mode == "local"}
    assert all(mode == "local" for mode, _ in controls[1:-1])
    chunks = [detail for mode, _, detail in probe.events if mode == "chunk"]
    expected_chunks = [65536] * (size // 65536)
    if size % 65536:
        expected_chunks.append(size % 65536)
    assert chunks == [*expected_chunks, 0]
    opens = [detail for mode, _, detail in probe.events if mode == "open"]
    assert all(flags & os.O_NOFOLLOW for _, flags in opens)
    assert len([name for name, _ in opens if name.startswith("d") and len(name) == 3]) == depth
    assert sum(detail[1] is False for mode, _, detail in probe.events if mode == "stat") >= 2 * (
        depth + 1
    )
    assert probe.control._origin is origin and probe.control._origin[3] == origin[3]
    probe.events.clear()
    probe.saved()
    assert _controls(probe.events) == [("full", "after-existing")]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
@pytest.mark.parametrize("name", ["success", *_ERROR_NAMES])
def test_unknown_control_matches_frozen_entire_io_trace_bytes_and_error_identity(
    file, monkeypatch, kind, name
):
    probe = _Probe(file)
    callback = _callback(probe, kind)
    if name != "success":
        probe.failure, probe.error = ("full", "block"), _error(name)
    _observe_real_reader(monkeypatch, probe)
    outcomes = []
    for implementation in (_frozen(), module._read_source_file):
        probe.phase, probe.chunks = "outside", 0
        probe.events.clear()
        if name == "success":
            result = _read(file, probe, implementation=implementation, callback=callback)
            assert result == (file.body, file.mode)
        else:
            with pytest.raises(BaseException) as caught:
                _read(file, probe, implementation=implementation, callback=callback)
            assert caught.value is probe.error
            result = caught.value
        outcomes.append((result, tuple(probe.events)))
    assert outcomes[0] == outcomes[1]
    assert not any(mode == "local" for mode, _, _ in probe.events)


def test_none_checkpoint_keeps_frozen_original_read_operation_and_io_trace(file, monkeypatch):
    probe = _Probe(file)
    _observe_real_reader(monkeypatch, probe)
    outcomes = []
    for implementation in (_frozen(), module._read_source_file):
        probe.phase, probe.chunks = "outside", 0
        probe.events.clear()
        result = implementation(file.root, file.path, "posix", None)
        assert result == (file.body, file.mode)
        outcomes.append((result, tuple(probe.events)))
    assert outcomes[0] == outcomes[1]
    assert _controls(probe.events) == []


@pytest.mark.parametrize("foreign", ["task", "thread"])
@pytest.mark.parametrize("name", ["success", *_ERROR_NAMES])
async def test_actual_foreign_file_keeps_frozen_full_trace_bytes_exception_and_origin(
    file, monkeypatch, foreign, name
):
    probe = _Probe(file)
    parent = probe.bind()
    origin = parent._origin
    if name != "success":
        probe.failure, probe.error = ("full", "block"), _error(name)
    _observe_real_reader(monkeypatch, probe)

    def run(implementation):
        if name == "success":
            return _read(file, probe, implementation=implementation)
        with pytest.raises(BaseException) as caught:
            _read(file, probe, implementation=implementation)
        assert caught.value is probe.error
        return caught.value

    results = []
    for implementation in (_frozen(), module._read_source_file):
        probe.phase, probe.chunks = "outside", 0
        probe.events.clear()
        if foreign == "thread":
            result = await asyncio.to_thread(run, implementation)
        else:

            async def child(implementation=implementation):
                return run(implementation)

            result = await asyncio.create_task(child())
        results.append((result, tuple(probe.events)))
    assert results[0] == results[1]
    if name == "success":
        assert results[0][0] == (file.body, file.mode)
    assert parent._origin is origin
    assert not any(mode == "local" for mode, _, _ in probe.events)


@pytest.mark.parametrize(
    "failure",
    [("full", "outside"), ("full", "after-existing"), ("local", "path"), ("local", "block")],
    ids=["entry-full", "exit-full", "path-local", "block-local"],
)
@pytest.mark.parametrize("name", _ERROR_NAMES)
def test_first_control_error_at_each_file_boundary_keeps_identity_and_has_no_later_io(
    file, monkeypatch, failure, name
):
    probe = _Probe(file)
    probe.failure, probe.error = failure, _error(name)
    _observe_real_reader(monkeypatch, probe)
    with pytest.raises(BaseException) as caught:
        _read(file, probe)
    assert caught.value is probe.error
    assert probe.events[-1][:2] == failure
    controls = _controls(probe.events)
    assert sum(mode == "full" for mode, _ in controls) == (
        2 if failure[1] == "after-existing" else 1
    )
    if failure[1] in {"outside", "path", "block"}:
        assert not any(mode == "read" for mode, _, _ in probe.events)


@pytest.mark.parametrize("name", _ERROR_NAMES)
def test_first_error_after_real_chunk_stops_next_read_and_revokes_saved_segment(
    file, monkeypatch, name
):
    probe = _Probe(file)
    probe.error = _error(name)
    probe.after_block = lambda: setattr(probe, "failure", ("local", "block"))
    origin = probe.control._origin
    _observe_real_reader(monkeypatch, probe)
    with pytest.raises(BaseException) as caught:
        _read(file, probe)
    assert caught.value is probe.error
    assert [detail for mode, _, detail in probe.events if mode == "chunk"] == [65536]
    assert sum(mode == "read" for mode, _, _ in probe.events) == 1
    assert probe.events[-1][:2] == ("local", "block")
    assert sum(mode == "full" for mode, _ in _controls(probe.events)) == 1
    assert probe.control._origin is origin
    probe.events.clear()
    probe.saved()
    assert _controls(probe.events) == [("full", "after-existing")]


def test_full_reentry_during_real_block_read_revokes_saved_local_without_origin_rebinding(
    file, monkeypatch
):
    probe = _Probe(file)
    origin = probe.control._origin

    def reenter():
        probe.record("reenter")
        probe.control()
        probe.saved()

    probe.after_block = reenter
    _observe_real_reader(monkeypatch, probe)
    assert _read(file, probe) == (file.body, file.mode)
    index = next(index for index, event in enumerate(probe.events) if event[0] == "reenter")
    assert any(mode == "local" for mode, _, _ in probe.events[:index])
    assert not any(mode == "local" for mode, _, _ in probe.events[index:])
    assert probe.control._origin is origin


@pytest.mark.parametrize("foreign", ["task", "thread"])
async def test_saved_active_native_segment_callback_is_revoked_by_real_foreign_call(file, foreign):
    # 原同步端口的生命周期负控；不把跨 await 的探针当作文件读取授权。
    probe = _Probe(file)
    probe.bind()
    origin = probe.control._origin
    with module._native_snapshot_progress(probe.control, same_task_only=True) as saved:
        saved()
        boundary = len(probe.events)
        if foreign == "thread":
            await asyncio.to_thread(saved)
        else:

            async def child():
                saved()

            await asyncio.create_task(child())
        saved()
    saved()
    assert _controls(probe.events[:boundary]) == [("full", "outside"), ("local", "outside")]
    assert not any(mode == "local" for mode, _, _ in probe.events[boundary:])
    assert probe.control._origin is origin


@pytest.mark.parametrize("field", ["_local_check", "_authenticate", "_task", "_thread"])
def test_parent_creation_fields_cannot_be_rebound_during_file_segment(file, monkeypatch, field):
    probe = _Probe(file)
    origin = probe.control._origin
    changed = False

    def change():
        nonlocal changed
        if not changed:
            changed = True
            setattr(probe.control, field, object())

    probe.local_hook = change
    _observe_real_reader(monkeypatch, probe)
    with pytest.raises(KernelError) as caught:
        _read(file, probe)
    assert caught.value.code == "git_authentication_control_invalid"
    assert probe.control._origin is origin
    assert not any(mode == "read" for mode, _, _ in probe.events)
    assert _controls(probe.events).count(("full", "after-existing")) == 0


@pytest.mark.parametrize("failure", ["cancel", "deadline", "locked-resource"])
def test_original_local_cancel_deadline_and_locked_resource_reject_before_next_read(
    file, monkeypatch, failure
):
    probe = _Probe(file)
    original_deadline = probe.deadline

    def change():
        if failure == "cancel":
            probe.cancel.cancel()
        elif failure == "deadline":
            probe.clock = lambda: original_deadline + 1
        else:
            probe.resource = object()

    probe.local_hook = change
    _observe_real_reader(monkeypatch, probe)
    kind = {"cancel": TurnCancelled, "deadline": TimeoutError, "locked-resource": KernelError}[
        failure
    ]
    with pytest.raises(kind) as caught:
        _read(file, probe)
    if failure == "locked-resource":
        assert caught.value is probe.resource_error
    assert probe.deadline == original_deadline
    assert probe.events[-1][:2] == ("local", "path")
    assert not any(mode == "read" for mode, _, _ in probe.events)
    assert len([mode for mode, _ in _controls(probe.events) if mode == "full"]) == 1


@pytest.mark.parametrize("failure", ["stopped", "native-deadline"])
def test_original_native_read_operation_keeps_stop_and_deadline_checks(file, monkeypatch, failure):
    original = NativeReadOperation.__init__

    def initialize(operation, checkpoint):
        original(operation, checkpoint)
        if failure == "stopped":
            operation.stopped.set()
        else:
            operation.deadline = time.monotonic() - 1

    monkeypatch.setattr(NativeReadOperation, "__init__", initialize)
    probe = _Probe(file)
    _observe_real_reader(monkeypatch, probe)
    with pytest.raises(TurnCancelled if failure == "stopped" else ReadToolError) as caught:
        _read(file, probe)
    if failure == "native-deadline":
        assert caught.value.code == "timeout"
    assert not any(mode == "read" for mode, _, _ in probe.events)
    assert len([mode for mode, _ in _controls(probe.events) if mode == "full"]) == 1


@pytest.mark.parametrize(
    "drift", ["owner-during-read", "owner-after-existing", "root-after-existing"]
)
def test_exit_full_refuses_original_owner_or_root_drift_even_after_body_was_returned(
    file, monkeypatch, drift
):
    probe = _Probe(file)

    def replace_root():
        file.root.rename(file.root.with_name("retired-root"))
        file.root.mkdir()

    if drift == "owner-during-read":
        probe.after_block = lambda: setattr(probe, "owner", object())
    elif drift == "owner-after-existing":
        probe.after_existing = lambda: setattr(probe, "owner", object())
    else:
        probe.after_existing = replace_root
    _observe_real_reader(monkeypatch, probe)
    with pytest.raises(KernelError) as caught:
        _read(file, probe)
    assert caught.value is (
        probe.root_error if drift == "root-after-existing" else probe.owner_error
    )
    assert _controls(probe.events)[-1] == ("full", "after-existing")
    assert [detail for mode, _, detail in probe.events if mode == "chunk"] == [65536, 65536, 137, 0]


@pytest.mark.parametrize(
    "damage,expected",
    [
        ("missing", "delivery_source_changed"),
        ("symlink", "path_denied"),
        ("parent-link", "path_denied"),
        ("hardlink", "path_denied"),
        ("fifo", "wrong_file_type"),
        ("directory", "wrong_file_type"),
        ("mode", "delivery_metadata_unsupported"),
        ("over-limit", "delivery_plan_limit"),
    ],
)
def test_real_io_loss_type_mode_and_size_denials_match_frozen_mapping(
    file, monkeypatch, damage, expected
):
    if damage == "missing":
        file.leaf.unlink()
    elif damage == "symlink":
        file.leaf.unlink()
        file.leaf.symlink_to(file.root / "absent-target")
    elif damage == "parent-link":
        directory = file.root / "d00"
        directory.rename(file.root / "retired-parent")
        directory.symlink_to(file.root / "retired-parent", target_is_directory=True)
    elif damage == "hardlink":
        os.link(file.leaf, file.root / "second-link")
    elif damage in {"fifo", "directory"}:
        file.leaf.unlink()
        if damage == "fifo":
            os.mkfifo(file.leaf)
        else:
            file.leaf.mkdir()
    elif damage == "mode":
        file.leaf.chmod(0o600)
    else:
        with file.leaf.open("r+b") as stream:
            stream.truncate(planner.MAX_TRANSACTION_FILE_BYTES + 1)
    probe = _Probe(file)
    _observe_real_reader(monkeypatch, probe)
    errors = []
    for implementation in (_frozen(), module._read_source_file):
        probe.phase, probe.chunks = "outside", 0
        probe.events.clear()
        with pytest.raises((KernelError, ReadToolError)) as caught:
            _read(file, probe, implementation=implementation)
        assert caught.value.code == expected
        errors.append((type(caught.value), caught.value.code))
    assert errors[0] == errors[1]
    assert _controls(probe.events).count(("full", "after-existing")) == 0


@pytest.mark.parametrize("damage", ["root", "parent", "leaf-change", "leaf-unlink"])
def test_real_mid_read_root_parent_and_file_changes_keep_original_fd_rechecks(
    tmp_path, monkeypatch, damage
):
    errors = []
    for label in ("before", "current"):
        file = _make_file(tmp_path / label / "root")
        probe = _Probe(file)

        def full(probe=probe):
            # 单独验证原生 FD 拒绝映射，不用认证根探针抢先覆盖原生错误。
            probe.record("full")

        probe.control = GitAuthenticationControl(probe.local, full)

        def change(file=file, probe=probe):
            probe.record("filesystem-change", damage)
            if damage == "root":
                file.root.rename(file.root.with_name("retired-root"))
                file.root.mkdir()
            elif damage == "parent":
                parent = file.root / "d00"
                parent.rename(file.root / "retired-parent")
                parent.mkdir()
            elif damage == "leaf-change":
                file.leaf.write_bytes(b"changed during actual chunk read")
            else:
                file.leaf.unlink()

        probe.after_block = change
        with monkeypatch.context() as patch:
            _observe_real_reader(patch, probe)
            implementation = _frozen() if label == "before" else module._read_source_file
            with pytest.raises((KernelError, ReadToolError)) as caught:
                _read(file, probe, implementation=implementation)
        errors.append((type(caught.value), caught.value.code))
        assert sum(mode == "filesystem-change" for mode, _, _ in probe.events) == 1
        assert _controls(probe.events).count(("full", "after-existing")) == 0
    assert errors[0] == errors[1] == (ReadToolError, "workspace_changed")
