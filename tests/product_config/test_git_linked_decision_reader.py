"""决定回读的窄控制测试；纯声明夹具不证明原业务资源或 MAC 已认证。"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_approval_history as subject
from harnessix.product_config.git_decision_link_rows import GitLinkHistory
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from tests.product_config.test_git_decided_source_reader import reader
from tests.product_config.test_git_decision_link_contracts import case as case
from tests.product_config.test_git_decision_link_sources import fixture


def arrange(monkeypatch, evidence, *, decision=True, terminal_error=None):
    calls = []
    fact = build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    histories = (GitLinkHistory(evidence.materials.link, fact if decision else None),)

    @contextmanager
    def control(resources, cancel, budget, checkpoint, read_set):
        calls.append(("control", cancel, budget))
        yield checkpoint
        calls.append("terminal")
        if terminal_error is not None:
            raise terminal_error
        assert read_set.linked == histories
        # 只模拟原 terminal 的新快照交付，不把替身算作来源认证。
        read_set.validated_result = replace(histories[0]).decision

    async def read_all(resources, cancel, budget, check, read_set):
        calls.append("all-stored-and-original-sources")
        check()
        return histories

    monkeypatch.setattr(subject, "_control", control)
    monkeypatch.setattr(subject, "_read_linked_all", read_all)
    return calls, fact


@pytest.mark.parametrize("kind", ["approved", "denied", "cancelled"])
def test_one_original_window_no_derivation_when_persisted_link_is_present(case, monkeypatch, kind):
    evidence = fixture(case, kind)
    calls, expected = arrange(monkeypatch, evidence)
    route = expected.plan.route.execution.plan_id
    cancel = CancelToken()
    assert (
        asyncio.run(reader().read_linked_decision(route, cancel=cancel, checkpoint=lambda: None))
        == expected
    )
    assert calls[0][1] is cancel and calls[0][2].remaining() <= 60
    assert calls[1:] == ["all-stored-and-original-sources", "terminal"]


@pytest.mark.parametrize("missing", ["route", "decision"])
def test_original_approval_is_not_used_to_fill_a_missing_persisted_link(case, monkeypatch, missing):
    evidence = fixture(case, "approved")
    calls, expected = arrange(monkeypatch, evidence, decision=missing != "decision")
    route = UUID(int=123) if missing == "route" else expected.plan.route.execution.plan_id
    with pytest.raises(KernelError) as caught:
        asyncio.run(
            reader().read_linked_decision(route, cancel=CancelToken(), checkpoint=lambda: None)
        )
    assert caught.value.code == "git_decision_link_missing"
    assert calls[1:] == ["all-stored-and-original-sources"]


@pytest.mark.parametrize("error", [TurnCancelled(), ValueError("original"), OSError("original")])
def test_terminal_first_error_prevents_return_and_preserves_identity(case, monkeypatch, error):
    evidence = fixture(case, "approved")
    _, expected = arrange(monkeypatch, evidence, terminal_error=error)
    with pytest.raises(type(error)) as caught:
        asyncio.run(
            reader().read_linked_decision(
                expected.plan.route.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
            )
        )
    assert caught.value is error


def test_non_target_persisted_decision_rechecked_at_original_terminal(case, monkeypatch):
    evidence = fixture(case, "approved")
    fact = build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    route = fact.plan.route.execution.plan_id
    forged = fact.model_copy(
        update={"request_event": fact.request_event.model_copy(update={"digest": "f" * 64})}
    )
    read_set = subject._DecidedReadSet()
    read_set.approvals[route] = evidence
    read_set.declaration = (UUID(int=555), fact)
    read_set.linked = (GitLinkHistory(evidence.materials.link, forged),)
    # 本用例只隔离验证新增来源比较；不替代原 super 的完整业务/SQL 终端测试。
    monkeypatch.setattr(subject._ApprovalReadSet, "terminal", lambda *args, **kwargs: None)
    with pytest.raises(KernelError) as caught:
        read_set.terminal(None, None, None, None, "fixture", lambda: None)
    assert caught.value.code == "git_decision_link_history_changed"
    assert read_set.validated_result is None


def test_non_target_foreign_equality_is_never_called_at_terminal(case, monkeypatch):
    evidence = fixture(case, "approved")
    fact = build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    calls = []

    class ForeignDecision:
        def __eq__(self, other):
            calls.append(other)
            return True

    read_set = subject._DecidedReadSet()
    read_set.approvals[fact.plan.route.execution.plan_id] = evidence
    read_set.declaration = (UUID(int=555), fact)
    read_set.linked = (GitLinkHistory(evidence.materials.link, ForeignDecision()),)
    monkeypatch.setattr(subject._ApprovalReadSet, "terminal", lambda *args, **kwargs: None)
    with pytest.raises(KernelError):
        read_set.terminal(None, None, None, None, "fixture", lambda: None)
    assert not calls and read_set.validated_result is None


@pytest.mark.parametrize("invalid", [None, "not-a-uuid", True])
def test_invalid_route_rejected_before_opening_control(monkeypatch, invalid):
    def must_not_open(*args, **kwargs):
        pytest.fail("无效 Route 不得进入原控制窗口")

    monkeypatch.setattr(subject, "_control", must_not_open)
    with pytest.raises(KernelError) as caught:
        asyncio.run(
            reader().read_linked_decision(invalid, cancel=CancelToken(), checkpoint=lambda: None)
        )
    assert caught.value.code == "git_decision_source_invalid"
