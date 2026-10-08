"""历史读取的显式纯计算端口；完整 CAS 与原控制异常不进入纯段。"""

from __future__ import annotations

import ast
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    same_task_pure_git_authentication,
)
from harnessix.workspace import parent_closure_codec as module
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.workspace.test_parent_closure_reader import _two_chunks
from tests.workspace.test_parent_closure_reader import history as history


class _Progress:
    def __init__(self):
        self.active = False
        self.saved = []
        self.events = []
        self.failure = None
        self.error = None

    def record(self, phase):
        self.events.append(phase)
        if self.failure == phase or self.failure == (phase, self.events.count(phase)):
            raise self.error

    def full(self):
        assert not self.active
        self.record("full")

    def local(self):
        assert self.active
        self.record("local")

    @contextmanager
    def pure(self):
        self.record("entry")
        self.active = True
        self.saved.append(self.local)
        try:
            yield self.local
        finally:
            self.active = False
        self.record("exit")


def _error(name):
    if name == "cancel":
        return TurnCancelled("original cancellation")
    if name == "task":
        return asyncio.CancelledError("original task cancellation")
    if name == "kernel":
        return KernelError("workspace_closure_corrupt", "control, not data")
    if name == "nested":
        return UpstreamCheckpointError(UpstreamCheckpointError(OSError("original")))
    return {"value": ValueError, "type": TypeError, "os": OSError}[name]("original control")


@pytest.mark.parametrize("split", [False, True])
def test_complete_reads_and_facts_match_default_with_all_cas_outside_pure(history, split):
    history = _two_chunks(history) if split else history
    progress = _Progress()
    default_reads, actual_reads = [], []

    def read(digest):
        assert not progress.active
        actual_reads.append(digest)
        progress.record("cas")
        return history.store._read_blob(digest)

    def default_read(digest):
        default_reads.append(digest)
        return history.store._read_blob(digest)

    expected = module.read_workspace_parent_closure(
        history.snapshot, default_read, checkpoint=lambda: None
    )
    actual = module.read_workspace_parent_closure(
        history.snapshot, read, checkpoint=progress.full, pure_progress=progress.pure
    )
    assert actual == expected == history.parents
    assert actual_reads == default_reads
    assert len(actual_reads) == (3 if split else 2)
    assert len(progress.saved) == len(actual_reads)
    assert progress.events.count("local") == 2 * len(history.parents)
    assert progress.events.count("entry") == progress.events.count("exit")


@pytest.mark.parametrize("phase", ["entry", "local", "exit", "full", "cas"])
@pytest.mark.parametrize("name", ["cancel", "task", "kernel", "value", "type", "os", "nested"])
def test_first_control_error_identity_never_becomes_data_error(history, phase, name):
    progress = _Progress()
    error = _error(name)
    progress.failure, progress.error = phase, error

    def read(digest):
        assert not progress.active
        progress.record("cas")
        return history.store._read_blob(digest)

    with pytest.raises(BaseException) as caught:
        module.read_workspace_parent_closure(
            history.snapshot, read, checkpoint=progress.full, pure_progress=progress.pure
        )
    assert caught.value is error
    assert progress.events[-1] == phase
    assert not progress.active


@pytest.mark.parametrize("segment", [1, 2, 3])
@pytest.mark.parametrize("phase", ["entry", "local-first", "local-last", "exit"])
@pytest.mark.parametrize("name", ["cancel", "task", "kernel", "value", "type", "os", "nested"])
def test_each_chunk_and_final_digest_first_failure_stops_at_exact_checkpoint(
    history, segment, phase, name
):
    history = _two_chunks(history)
    progress = _Progress()
    counts = [item["count"] for item in history.manifest()["chunks"]]
    counts.append(len(history.parents))
    mode = "local" if phase.startswith("local") else phase
    call = (
        sum(counts[: segment - 1]) + (1 if phase == "local-first" else counts[segment - 1])
        if mode == "local"
        else segment
    )
    error = _error(name)
    progress.failure, progress.error = (mode, call), error
    reads = []

    def read(digest):
        assert not progress.active
        reads.append(digest)
        return history.store._read_blob(digest)

    with pytest.raises(BaseException) as caught:
        module.read_workspace_parent_closure(
            history.snapshot, read, checkpoint=progress.full, pure_progress=progress.pure
        )
    assert caught.value is error and not progress.active
    assert len(reads) == min(segment + 1, 3)
    assert progress.events[-1] == mode and progress.events.count(mode) == call
    assert progress.events.count("exit") == (segment if phase == "exit" else segment - 1)


@pytest.mark.parametrize("boundary", ["manifest", "chunk", "after-pure"])
def test_binding_change_fails_at_original_full_boundary_without_next_read(history, boundary):
    reads, saved = [], []
    changed = False
    error = KernelError("git_source_owner_changed", boundary)

    def full():
        if changed:
            raise error

    control = GitAuthenticationControl(lambda: None, full)

    @contextmanager
    def pure():
        nonlocal changed
        with same_task_pure_git_authentication(control) as local:
            saved.append(local)
            yield local
            if boundary == "after-pure":
                changed = True

    def read(digest):
        nonlocal changed
        reads.append(digest)
        body = history.store._read_blob(digest)
        if len(reads) == (1 if boundary == "manifest" else 2) and boundary != "after-pure":
            changed = True
        return body

    with pytest.raises(KernelError) as caught:
        module.read_workspace_parent_closure(
            history.snapshot, read, checkpoint=control, pure_progress=pure
        )
    assert caught.value is error
    assert len(reads) == (1 if boundary == "manifest" else 2)
    assert bool(saved) is (boundary == "after-pure")


@pytest.mark.parametrize("foreign", ["thread", "task"])
def test_foreign_control_retains_complete_default_callback_count(history, foreign):
    full_calls, local_calls = [], []
    control = GitAuthenticationControl(lambda: local_calls.append(1), lambda: full_calls.append(1))

    def run(layered):
        full_calls.clear()
        options = (
            {"pure_progress": lambda: same_task_pure_git_authentication(control)} if layered else {}
        )
        result = module.read_workspace_parent_closure(
            history.snapshot, history.store._read_blob, checkpoint=control, **options
        )
        return result, len(full_calls)

    if foreign == "thread":
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(run, False).result()
            current = pool.submit(run, True).result()
    else:

        async def other():
            return run(False), run(True)

        old, current = asyncio.run(other())
    assert old == current and old[0] == history.parents
    assert not local_calls


def test_saved_local_is_revoked_and_late_chunk_corruption_never_returns_prefix(history):
    history = _two_chunks(history)
    full_calls, local_calls, saved, reads = [], [], [], []
    control = GitAuthenticationControl(lambda: local_calls.append(1), lambda: full_calls.append(1))

    @contextmanager
    def pure():
        with same_task_pure_git_authentication(control) as check:
            saved.append(check)
            yield check

    def read(digest):
        reads.append(digest)
        body = history.store._read_blob(digest)
        return body if len(reads) < 3 else body + b"corrupt"

    with pytest.raises(KernelError) as caught:
        module.read_workspace_parent_closure(
            history.snapshot, read, checkpoint=control, pure_progress=pure
        )
    assert caught.value.code == "workspace_closure_corrupt"
    assert len(reads) == 3 and len(saved) == 1 and local_calls
    before = len(full_calls), len(local_calls)
    saved[0]()
    assert (len(full_calls), len(local_calls)) == (before[0] + 1, before[1])


def test_default_trace_and_wire_match_frozen_reader(history):
    directory = os.environ.get("HARNESSIX_CODEC_FROZEN_DIR")
    assert directory is not None, "验收必须提供旧实现原件，不能跳过 oracle"
    path = Path(directory) / "src/harnessix/workspace/parent_closure_codec.py"
    function = next(
        node
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "read_workspace_parent_closure"
    )
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    trace = []

    def read(digest):
        trace.append(("cas", digest))
        return history.store._read_blob(digest)

    def check():
        trace.append("full")

    expected = namespace[function.name](history.snapshot, read, checkpoint=check)
    old_trace = list(trace)
    trace.clear()
    actual = module.read_workspace_parent_closure(
        history.snapshot, read, checkpoint=check, pure_progress=None
    )
    assert actual == expected == history.parents
    assert trace == old_trace
