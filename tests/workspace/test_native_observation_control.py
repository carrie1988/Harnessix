"""以真实 POSIX 观察验证父操作检查、原字节兼容及句柄回收。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.tools import workspace as workspace_io
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS, ReadToolError
from harnessix.tools.workspace import ReadOperation, revision_state
from harnessix.workspace import snapshot as module
from harnessix.workspace.contracts import ResourceAccess

pytestmark = pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 原生 IO")
_CONTROL_SIGNALS = (
    pytest.param(TurnCancelled, id="turn-cancelled"),
    pytest.param(asyncio.CancelledError, id="task-cancelled"),
    pytest.param(TimeoutError, id="parent-timeout"),
    pytest.param(lambda: ReadToolError("timeout"), id="parent-read-timeout"),
)
_PAYLOAD = b"native-observation\n" * 12_000


class _IOTrace:
    """旁观真实 IO，不替换观察、文件内容、目录成员或操作返回值。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.live: dict[int, str] = {}
        self.opened: list[str] = []
        self.reads: list[int] = []
        self.members: list[str] = []
        self.active_scans = 0
        self.completed_scans = 0
        self._open = os.open
        self._close = os.close
        self._read = os.read
        self._scandir = os.scandir
        monkeypatch.setattr(os, "open", self.open)
        monkeypatch.setattr(os, "close", self.close)
        monkeypatch.setattr(os, "read", self.read)
        monkeypatch.setattr(os, "scandir", self.scandir)

    def open(
        self, path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = self._open(path, flags, mode, dir_fd=dir_fd)
        self.live[descriptor] = str(path)
        self.opened.append(str(path))
        return descriptor

    def close(self, descriptor: int) -> None:
        self._close(descriptor)
        self.live.pop(descriptor)

    def read(self, descriptor: int, count: int) -> bytes:
        body = self._read(descriptor, count)
        self.reads.append(len(body))
        return body

    @contextmanager
    def scandir(self, descriptor: int) -> Iterator[Iterator[os.DirEntry[str]]]:
        self.active_scans += 1
        try:
            with self._scandir(descriptor) as iterator:
                yield self._entries(iterator)
        finally:
            self.active_scans -= 1
            self.completed_scans += 1

    def _entries(self, iterator: Iterator[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
        for entry in iterator:
            self.members.append(entry.name)
            yield entry


@contextmanager
def _tracked_native(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[module._PosixRoot, _IOTrace]]:
    trace = _IOTrace(monkeypatch)
    native = module._PosixRoot(root)
    retained = native._workspace._root_fd
    assert retained is not None
    try:
        yield native, trace
        assert set(trace.live) == {retained}
        assert os.fstat(retained).st_ino == native.root_identity[1]
        assert trace.active_scans == 0
    finally:
        native.close()
        assert trace.live == {}
        assert trace.active_scans == 0
        with pytest.raises(OSError) as error:
            os.fstat(retained)
        assert error.value.errno == errno.EBADF


def _facts(observed: module._Observed) -> tuple[object, ...]:
    return observed.kind, observed.identity, observed.content, observed.size, observed.entries


def _deep_file(root: Path) -> str:
    path = "one/two/three/value"
    target = root / path
    target.parent.mkdir(parents=True)
    target.write_bytes(_PAYLOAD)
    return path


@pytest.mark.parametrize("stop_item", [1, 2, 3, 4])
@pytest.mark.parametrize("signal", _CONTROL_SIGNALS)
def test_directory_each_member_consumes_parent_control_and_closes_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_item: int,
    signal: Callable[[], BaseException],
) -> None:
    for name in ("z", "中", "a", "dir"):
        (tmp_path / name).mkdir()
    with _tracked_native(tmp_path, monkeypatch) as (native, trace):
        baseline = native.observe(".", access="read")
        assert baseline.kind == "directory" and baseline.size == 4
        trace.members.clear()
        checked_items: list[int] = []

        def successful_checkpoint() -> None:
            if trace.active_scans:
                checked_items.append(len(trace.members))

        successful = native.observe(".", access="read", checkpoint=successful_checkpoint)
        assert _facts(successful) == _facts(baseline)
        assert checked_items == [1, 2, 3, 4]
        trace.members.clear()
        control_error = signal()

        def checkpoint() -> None:
            if trace.active_scans and len(trace.members) == stop_item:
                raise control_error

        with pytest.raises(type(control_error)) as error:
            native.observe(".", access="read", checkpoint=checkpoint)
        assert error.value is control_error
        assert len(trace.members) == stop_item
        assert trace.completed_scans == 3


@pytest.mark.parametrize("stop_chunk", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("signal", _CONTROL_SIGNALS)
def test_file_each_chunk_consumes_parent_control_and_closes_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_chunk: int,
    signal: Callable[[], BaseException],
) -> None:
    (tmp_path / "value").write_bytes(_PAYLOAD)
    with _tracked_native(tmp_path, monkeypatch) as (native, trace):
        baseline = native.observe("value", access="read")
        assert baseline.kind == "file" and baseline.content == _PAYLOAD
        assert trace.reads == [65536, 65536, 65536, len(_PAYLOAD) - 3 * 65536, 0]
        trace.reads.clear()
        checked_chunks: list[int] = []

        def successful_checkpoint() -> None:
            if "value" in trace.live.values():
                checked_chunks.append(len(trace.reads))

        successful = native.observe("value", access="read", checkpoint=successful_checkpoint)
        assert _facts(successful) == _facts(baseline)
        assert set(range(5)) <= set(checked_chunks)
        trace.reads.clear()
        control_error = signal()

        def checkpoint() -> None:
            if "value" in trace.live.values() and len(trace.reads) == stop_chunk:
                raise control_error

        with pytest.raises(type(control_error)) as error:
            native.observe("value", access="read", checkpoint=checkpoint)
        assert error.value is control_error
        assert len(trace.reads) == stop_chunk


@pytest.mark.parametrize("stop_depth", [0, 1, 2, 3])
@pytest.mark.parametrize("signal", _CONTROL_SIGNALS)
def test_root_and_workspace_handle_chain_consumes_parent_control_and_closes_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_depth: int,
    signal: Callable[[], BaseException],
) -> None:
    path = _deep_file(tmp_path)
    components = {"one", "two", "three"}
    with _tracked_native(tmp_path, monkeypatch) as (native, trace):
        checked_depths: list[int] = []

        def successful_checkpoint() -> None:
            checked_depths.append(len(components.intersection(trace.live.values())))

        successful = native.observe(path, access="read", checkpoint=successful_checkpoint)
        assert successful.kind == "file" and successful.content == _PAYLOAD
        assert set(checked_depths) == {0, 1, 2, 3}
        trace.opened.clear()
        trace.reads.clear()
        control_error = signal()

        def checkpoint() -> None:
            if len(components.intersection(trace.live.values())) == stop_depth:
                raise control_error

        with pytest.raises(type(control_error)) as error:
            native.observe(path, access="read", checkpoint=checkpoint)
        assert error.value is control_error
        assert trace.reads == []
        assert components.intersection(trace.opened) == set(("one", "two", "three")[:stop_depth])
        if stop_depth == 0:
            assert trace.opened == []


@pytest.mark.parametrize("signal", _CONTROL_SIGNALS)
def test_parent_control_survives_handle_chain_post_read_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal: Callable[[], BaseException]
) -> None:
    path = _deep_file(tmp_path)
    with _tracked_native(tmp_path, monkeypatch) as (native, trace):
        assert native.observe(path, access="read").content == _PAYLOAD
        trace.reads.clear()
        control_error = signal()
        post_read_checks = 0

        def checkpoint() -> None:
            nonlocal post_read_checks
            if trace.reads and trace.reads[-1] == 0:
                post_read_checks += 1
                if post_read_checks == 2:
                    raise control_error

        with pytest.raises(type(control_error)) as error:
            native.observe(path, access="read", checkpoint=checkpoint)
        assert error.value is control_error
        assert post_read_checks == 2
        assert sum(trace.reads) == len(_PAYLOAD)


@pytest.mark.parametrize("path", [".", "dir", "dir/文件", "dir/absent"])
@pytest.mark.parametrize("access", ["read", "write", "execute"])
def test_v1_without_checkpoint_matches_controlled_native_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, access: ResourceAccess
) -> None:
    (tmp_path / "dir").mkdir()
    target = tmp_path / "dir/文件"
    target.write_bytes(_PAYLOAD)
    target.chmod(0o755)
    with _tracked_native(tmp_path, monkeypatch) as (native, _trace):
        calls = 0

        def checkpoint() -> None:
            nonlocal calls
            calls += 1

        original = native.observe(path, access=access)
        controlled = native.observe(path, access=access, checkpoint=checkpoint)
        explicit_none = native.observe(path, access=access, checkpoint=None)
        assert _facts(original) == _facts(controlled) == _facts(explicit_none)
        assert calls > 0


def test_directory_keeps_v1_json_identity_sorting_and_no_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "中").write_bytes(b"native\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "z").symlink_to(tmp_path / "中")
    os.mkfifo(tmp_path / "fifo")
    info = tmp_path.stat()
    entries = []
    for name in ("中", "a", "z", "fifo"):
        child = (tmp_path / name).lstat()
        entries.append((name, stat.S_IFMT(child.st_mode), (child.st_dev, child.st_ino)))
    body = json.dumps(sorted(entries), ensure_ascii=False, separators=(",", ":")).encode()
    expected_identity = (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        hashlib.sha256(body).hexdigest(),
    )
    with _tracked_native(tmp_path, monkeypatch) as (native, _trace):
        for checkpoint in (None, lambda: None):
            observed = native.observe(".", access="read", checkpoint=checkpoint)
            assert observed.content == body and observed.identity == expected_identity
            assert observed.size == 4
            assert observed.entries == (
                ("a", "directory"),
                ("fifo", "special"),
                ("z", "symlink"),
                ("中", "file"),
            )
            file = native.observe("中", access="read", checkpoint=checkpoint)
            assert file.identity == revision_state((tmp_path / "中").stat())
            assert file.content == b"native\n" and file.size == 7


def test_v1_directory_does_not_add_local_per_member_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original_checkpoint = ReadOperation.checkpoint

    def checkpoint(operation: ReadOperation) -> None:
        nonlocal calls
        calls += 1
        original_checkpoint(operation)

    monkeypatch.setattr(ReadOperation, "checkpoint", checkpoint)
    with _tracked_native(tmp_path, monkeypatch) as (native, _trace):
        calls = 0
        assert native.observe(".", access="read").size == 0
        empty_calls = calls
        for index in range(4):
            (tmp_path / str(index)).write_bytes(b"x")
        calls = 0
        assert native.observe(".", access="read").size == 4
        assert calls == empty_calls == 2


@pytest.mark.parametrize("phase", ["directory", "file", "chain"])
def test_original_local_read_timeout_remains_active_with_parent_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    path = _deep_file(tmp_path)
    now = [100.0]
    monkeypatch.setattr(workspace_io.time, "monotonic", lambda: now[0])
    with _tracked_native(tmp_path, monkeypatch) as (native, trace):
        assert native.observe(path, access="read").content == _PAYLOAD
        trace.reads.clear()

        def checkpoint() -> None:
            reached = {
                "directory": trace.active_scans > 0,
                "file": len(trace.reads) == 1,
                "chain": "one" in trace.live.values(),
            }[phase]
            if reached:
                now[0] += READ_TIMEOUT_SECONDS + 1

        with pytest.raises(KernelError) as error:
            native.observe(
                "." if phase == "directory" else path, access="read", checkpoint=checkpoint
            )
        assert error.value.code == "workspace_timeout"


def test_multiple_observations_do_not_reset_parent_absolute_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "value").write_bytes(_PAYLOAD)
    now = [100.0]
    monkeypatch.setattr(workspace_io.time, "monotonic", lambda: now[0])
    parent = ReadOperation(timeout_seconds=2)
    deadline = parent.deadline
    with _tracked_native(tmp_path, monkeypatch) as (native, _trace):
        assert native.observe(".", access="read", checkpoint=parent.checkpoint).kind == "directory"
        now[0] += 1
        assert (
            native.observe("value", access="read", checkpoint=parent.checkpoint).content == _PAYLOAD
        )
        assert parent.deadline == deadline
        now[0] += 1
        with pytest.raises(ReadToolError) as error:
            native.observe(".", access="read", checkpoint=parent.checkpoint)
        assert error.value.code == "timeout"
        assert parent.deadline == deadline


@pytest.mark.parametrize("controlled", [False, True])
def test_real_directory_retains_ten_thousand_entry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controlled: bool
) -> None:
    limit = module.MAX_SNAPSHOT_DIRECTORY_ENTRIES
    assert limit == 10_000
    for index in range(limit):
        (tmp_path / f"f{index:05}").touch()
    with _tracked_native(tmp_path, monkeypatch) as (native, trace):
        checked_items = 0

        def checkpoint() -> None:
            nonlocal checked_items
            if trace.active_scans:
                checked_items += 1

        check = checkpoint if controlled else None
        successful = native.observe(".", access="read", checkpoint=check)
        assert successful.kind == "directory" and successful.size == limit
        assert successful.entries is not None and len(successful.entries) == limit
        assert checked_items == (limit if controlled else 0)
        (tmp_path / "overflow").touch()
        with pytest.raises(KernelError) as error:
            native.observe(".", access="read", checkpoint=check)
        assert error.value.code == "workspace_snapshot_limit"
        assert checked_items == (2 * limit + 1 if controlled else 0)


@pytest.mark.parametrize("path", ["linked-file", "linked-dir", "linked-dir/value"])
def test_controlled_observation_still_rejects_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    (tmp_path / "actual").mkdir()
    (tmp_path / "actual/value").write_bytes(_PAYLOAD)
    (tmp_path / "linked-file").symlink_to(tmp_path / "actual/value")
    (tmp_path / "linked-dir").symlink_to(tmp_path / "actual", target_is_directory=True)
    with _tracked_native(tmp_path, monkeypatch) as (native, _trace):
        assert (
            native.observe("actual/value", access="read", checkpoint=lambda: None).content
            == _PAYLOAD
        )
        with pytest.raises(KernelError) as error:
            native.observe(path, access="read", checkpoint=lambda: None)
        assert error.value.code == "workspace_path_denied"
