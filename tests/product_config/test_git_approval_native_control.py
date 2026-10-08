"""审批 Reader 转发原 Ledger 只读观察；仅检验接缝，不宣称真实恢复通过。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.product_config import git_prepared_approval_history as approval
from harnessix.product_config.git_delivery_process import GitOperationBudget


@pytest.mark.parametrize("qualified", [False, True])
async def test_approval_forwards_same_operation_observer_and_original_scope(monkeypatch, qualified):
    resources = SimpleNamespace(
        _router=object(),
        _core_store=SimpleNamespace(store=object()),
        _artifacts=SimpleNamespace(session=object()),
        _ports=object(),
        _workspace_scope="scope",
        _reader=object(),
    )
    route_id, observation, history, source_scope = object(), object(), object(), object()
    link = SimpleNamespace(
        plan=SimpleNamespace(
            core=SimpleNamespace(user_observation=observation),
            route=SimpleNamespace(execution=SimpleNamespace(plan_id=route_id)),
        )
    )
    evidence = SimpleNamespace(materials=SimpleNamespace(history=history))
    read_set = SimpleNamespace(source_scope=source_scope, evidence={}, approvals={})
    cancel, budget, trace = CancelToken(), GitOperationBudget(60), []
    observer = (lambda: trace.append("native")) if qualified else None

    def check():
        trace.append("full")

    async def read(actual, *args, **kwargs):
        assert actual is link and kwargs["checkpoint"] is check
        trace.append("proof")
        return evidence

    def issue(actual, actual_cancel, actual_budget, actual_check):
        assert (actual, actual_cancel, actual_budget, actual_check) == (
            resources,
            cancel,
            budget,
            check,
        )
        trace.append("observer")
        return observer

    async def verify(actual, actual_history, *args, **kwargs):
        assert actual is observation and actual_history is history
        assert kwargs["native_observer"] is observer and kwargs["source_scope"] is source_scope
        assert (
            kwargs["checkpoint"] is check
            and kwargs["budget"] is budget
            and kwargs["cancel"] is cancel
        )
        trace.append("U")

    monkeypatch.setattr(approval, "read_original_approval_evidence", read)
    monkeypatch.setattr(approval, "_native_user_observer", issue, raising=False)
    monkeypatch.setattr(approval, "verify_product_git_user_observation", verify)
    assert (
        await approval._read_evidence(resources, link, cancel, budget, check, read_set) is evidence
    )
    assert trace == ["proof", "observer", "U", "full"]
    assert read_set.evidence == {route_id: evidence.materials} and read_set.approvals == {
        route_id: evidence
    }
