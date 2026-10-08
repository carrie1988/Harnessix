"""进度运输只标记控制一层；主体首错误不能被清理覆盖或被上下文抑制。"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from harnessix.agent.errors import KernelError
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import (
    observed_workspace_pure_progress,
    protected_workspace_pure_progress,
)


@pytest.mark.parametrize("cleanup", ["raise", "suppress", "normal"])
@pytest.mark.parametrize("nested", [False, True])
def test_body_first_error_keeps_exact_object_even_when_cleanup_overrides(cleanup, nested):
    body = KernelError("workspace_closure_corrupt", "actual body error")
    if nested:
        body = UpstreamCheckpointError(UpstreamCheckpointError(body))

    @contextmanager
    def factory():
        try:
            yield lambda: None
        except BaseException:
            if cleanup == "raise":
                raise OSError("later cleanup failure") from None
            if cleanup != "suppress":
                raise

    with pytest.raises(BaseException) as caught:
        with protected_workspace_pure_progress(factory)():
            raise body
    assert caught.value is body


@pytest.mark.parametrize("where", ["create", "entry", "local", "exit"])
@pytest.mark.parametrize("nested", [False, True])
def test_each_control_origin_has_exactly_one_transport_layer(where, nested):
    error = KernelError("workspace_closure_corrupt", "control with same data code")
    if nested:
        error = UpstreamCheckpointError(UpstreamCheckpointError(error))

    @contextmanager
    def context():
        if where == "entry":
            raise error

        def local():
            if where == "local":
                raise error

        yield local
        if where == "exit":
            raise error

    def factory():
        if where == "create":
            raise error
        return context()

    with pytest.raises(UpstreamCheckpointError) as caught:
        with protected_workspace_pure_progress(factory)() as check:
            check()
    assert type(caught.value) is UpstreamCheckpointError and caught.value.error is error


def test_observer_runs_before_caller_and_can_keep_original_read_io(tmp_path):
    path = tmp_path / "original-observer-input"
    path.write_bytes(b"original")
    trace = []

    def observer():
        assert path.read_bytes() == b"original"
        trace.append("observer")

    @contextmanager
    def factory():
        yield lambda: trace.append("caller")

    with observed_workspace_pure_progress(factory, observer)() as check:
        check()
        check()
    assert trace == ["observer", "caller"] * 2
