"""原 Reader 新入口的窄控制测试；替身只验证接线，不声明 MAC 认证通过。"""

import asyncio
import inspect
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_approval_history as subject

ROUTE = UUID(int=31)


def reader():
    return subject.ProductGitPreparedApprovalHistoryReader(
        object(),
        object(),
        object(),
        object(),
        object(),
        snapshot_ports=object(),
        workspace_scope="1" * 64,
    )


def arrange(monkeypatch, *, state="approved", present=True, terminal_error=None, map_error=None):
    calls = []
    evidence = SimpleNamespace(projection=SimpleNamespace(state=state))
    result = object()

    @contextmanager
    def control(resources, cancel, budget, checkpoint, read_set):
        calls.append(("control", cancel, budget))
        checkpoint()
        yield checkpoint
        calls.append("terminal")
        if terminal_error is not None:
            raise terminal_error
        # 此替身只模拟成功终端交付，不证明原来源认证。
        read_set.validated_result = read_set.declaration[1]

    async def read_all(resources, cancel, budget, check, read_set):
        calls.append("all-links/U/material/review")
        if present:
            read_set.approvals[ROUTE] = evidence
        check()
        return ()

    def mapper(actual, *, checkpoint):
        assert actual is evidence
        calls.append("map")
        checkpoint()
        if map_error is not None:
            raise map_error
        return result

    monkeypatch.setattr(subject, "_control", control)
    monkeypatch.setattr(subject, "_read_all", read_all)
    monkeypatch.setattr(subject, "build_git_decision_link_sources", mapper, raising=False)
    return calls, result


@pytest.mark.parametrize("state", ["approved", "denied", "cancelled"])
def test_closed_read_uses_one_original_window_then_terminal(monkeypatch, state):
    calls, expected = arrange(monkeypatch, state=state)
    cancel = CancelToken()
    actual = asyncio.run(reader().read_decided(ROUTE, cancel=cancel, checkpoint=lambda: None))
    assert actual is expected
    assert calls[0][1] is cancel
    assert [entry if isinstance(entry, str) else entry[0] for entry in calls] == [
        "control",
        "all-links/U/material/review",
        "map",
        "terminal",
    ]
    assert calls[0][2].remaining() <= 60.0


@pytest.mark.parametrize(
    "present,state,code",
    [
        (False, "approved", "git_decision_source_missing"),
        (True, "pending", "git_decision_source_pending"),
    ],
)
def test_missing_or_pending_is_not_a_decided_fact(monkeypatch, present, state, code):
    calls, _ = arrange(monkeypatch, present=present, state=state)
    with pytest.raises(KernelError) as caught:
        asyncio.run(reader().read_decided(ROUTE, cancel=CancelToken(), checkpoint=lambda: None))
    assert caught.value.code == code
    assert "map" not in calls


@pytest.mark.parametrize(
    "error",
    [ValueError("control"), TypeError("control"), OSError("control"), TurnCancelled()],
)
def test_terminal_failure_prevents_result_and_preserves_identity(monkeypatch, error):
    calls, _ = arrange(monkeypatch, terminal_error=error)
    with pytest.raises(type(error)) as caught:
        asyncio.run(reader().read_decided(ROUTE, cancel=CancelToken(), checkpoint=lambda: None))
    assert caught.value is error and calls[-1] == "terminal"


@pytest.mark.parametrize(
    "error",
    [ValueError("mapping"), TypeError("mapping"), OSError("mapping"), TurnCancelled()],
)
def test_mapping_control_failure_preserves_identity(monkeypatch, error):
    arrange(monkeypatch, map_error=error)
    with pytest.raises(type(error)) as caught:
        asyncio.run(reader().read_decided(ROUTE, cancel=CancelToken(), checkpoint=lambda: None))
    assert caught.value is error


def test_invalid_route_never_executes_foreign_comparison(monkeypatch):
    calls = []

    class Foreign:
        def __eq__(self, other):
            calls.append(other)
            return True

    with pytest.raises(KernelError):
        asyncio.run(reader().read_decided(Foreign(), cancel=CancelToken(), checkpoint=lambda: None))
    assert calls == []


def test_real_control_rejects_non_original_resources_without_mapping(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("无原宿主不得进入 mapper")

    monkeypatch.setattr(subject, "build_git_decision_link_sources", forbidden, raising=False)
    with pytest.raises(KernelError) as caught:
        asyncio.run(reader().read_decided(ROUTE, cancel=CancelToken(), checkpoint=lambda: None))
    assert caught.value.code == "git_prepared_link_changed"


def test_input_has_no_evidence_hash_approval_or_authtoken():
    parameters = tuple(
        inspect.signature(subject.ProductGitPreparedApprovalHistoryReader.read_decided).parameters
    )
    assert parameters == ("self", "route_id", "cancel", "checkpoint")


@pytest.mark.parametrize("expired", [False, True])
def test_upstream_timeout_identity_is_not_operation_expiry(monkeypatch, expired):
    arrange(monkeypatch)
    upstream = TimeoutError("original-upstream")

    class Window:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def expired(self):
            return expired

    async def fail(*args, **kwargs):
        raise upstream

    monkeypatch.setattr(subject.asyncio, "timeout", lambda remaining: Window())
    monkeypatch.setattr(subject, "_read_all", fail)
    with pytest.raises(KernelError if expired else TimeoutError) as caught:
        asyncio.run(reader().read_decided(ROUTE, cancel=CancelToken(), checkpoint=lambda: None))
    if expired:
        assert caught.value.code == "git_process_timeout"
    else:
        assert caught.value is upstream


def test_non_target_failure_precedes_missing_target_and_mapping(monkeypatch):
    calls, _ = arrange(monkeypatch, present=False)
    original = KernelError("git_delivery_store_unproven", "非目标关联认证失败")

    async def complete_read(resources, cancel, budget, check, read_set):
        raise original

    monkeypatch.setattr(subject, "_read_all", complete_read)
    with pytest.raises(KernelError) as caught:
        asyncio.run(reader().read_decided(ROUTE, cancel=CancelToken(), checkpoint=lambda: None))
    assert caught.value is original and "map" not in calls
