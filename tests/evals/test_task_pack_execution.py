from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import utc_now
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.contracts import CodingEvalEnvironment, CodingEvalRunState
from harnessix.evals.report import (
    eval_report_sha256,
    read_eval_campaign_execution_state,
    read_eval_campaign_plan,
)
from harnessix.evals.suite_contracts import CodingEvalSuiteCasePlan
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_contracts import CodingEvalTaskPackCase
from harnessix.evals.task_pack_execution import TaskPackCaseExecutor
from harnessix.evals.task_pack_trial import _profile_observations, _require_completed_trial
from harnessix.models.pricing import BillingContext, FlatInputPrice, PriceSnapshot
from tests.evals.test_campaign import evidence
from tests.evals.test_grader import completed_turn, task

_MODEL = "harnessix-recorded-v1"


def _case_and_campaign():
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    case = loaded.manifest.case("agents-normalize-tool-name")
    created_at = utc_now()
    price = PriceSnapshot(
        version="task-pack-recorded-v1",
        source_url="https://harnessix.invalid/pricing/recorded-v1",
        billing_provider="harnessix-recorded",
        model=_MODEL,
        region="offline",
        service_tier="eval",
        inference_mode="recorded",
        currency="USD",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        input_tokens_min=0,
        input_tokens_max=None,
        input_price=FlatInputPrice(per_million="0"),
        output_per_million="0",
    )
    campaign = CodingEvalCampaignPlan(
        campaign_id=uuid4(),
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        environment=CodingEvalEnvironment(
            harnessix_revision="a" * 40,
            provider="recorded",
            model=_MODEL,
            platform="test-posix",
            isolation="fixed-container-no-network",
        ),
        run_ids=(uuid4(), uuid4()),
        price=price,
        billing_context=BillingContext(
            billing_provider="harnessix-recorded",
            region="offline",
            service_tier="eval",
            inference_mode="recorded",
        ),
        created_at=created_at,
    )
    expected = CodingEvalSuiteCasePlan(
        case_id=case.case_id,
        task_kind=case.task_kind,
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        repository=case.task.repository,
        campaign_plan_fingerprint=campaign.fingerprint,
    )
    return loaded, expected, campaign


async def test_case_adapter_persists_plan_before_cancelled_trial_and_reopens(
    tmp_path: Path,
) -> None:
    loaded, expected, campaign = _case_and_campaign()

    def provider_factory(*_args):
        @asynccontextmanager
        async def context():
            raise AssertionError("边界取消不得创建Provider")
            yield

        return context()

    root = tmp_path / "case"
    root.mkdir(mode=0o700)
    executor = TaskPackCaseExecutor(
        loaded,
        Path("/missing/git"),
        Path("/missing/docker"),
        provider_factory,
    )
    token = CancelToken()
    token.cancel()

    with pytest.raises(TurnCancelled):
        await executor(expected, campaign, root, token)

    assert read_eval_campaign_plan(root / "campaign-plan.json") == campaign
    state = read_eval_campaign_execution_state(root / "campaign-state.json")
    assert state.status == "ready" and not state.completed_run_ids
    with pytest.raises(TurnCancelled):
        await executor(expected, campaign, root, token)
    assert read_eval_campaign_execution_state(root / "campaign-state.json") == state


async def test_case_adapter_rejects_provider_binding_drift_before_provider(
    tmp_path: Path,
) -> None:
    loaded, expected, campaign = _case_and_campaign()

    def provider_factory(*_args):
        @asynccontextmanager
        async def context():
            raise AssertionError("取消或绑定漂移不得创建Provider")
            yield

        return context()

    root = tmp_path / "case"
    root.mkdir(mode=0o700)
    token = CancelToken()
    token.cancel()
    initial = TaskPackCaseExecutor(
        loaded,
        Path("/missing/git"),
        Path("/missing/docker"),
        provider_factory,
        provider_binding_sha256="a" * 64,
    )
    with pytest.raises(TurnCancelled):
        await initial(expected, campaign, root, token)

    drifted = TaskPackCaseExecutor(
        loaded,
        Path("/missing/git"),
        Path("/missing/docker"),
        provider_factory,
        provider_binding_sha256="b" * 64,
    )
    with pytest.raises(KernelError, match="Campaign状态漂移"):
        await drifted(expected, campaign, root, CancelToken())


async def test_case_adapter_rejects_suite_identity_drift_before_execution(
    tmp_path: Path,
) -> None:
    loaded, expected, campaign = _case_and_campaign()
    drifted = expected.model_copy(update={"task_fingerprint": "f" * 64})
    executor = TaskPackCaseExecutor(
        loaded,
        Path("/missing/git"),
        Path("/missing/docker"),
        lambda *_: pytest.fail("身份漂移不得创建Provider"),
    )

    with pytest.raises(KernelError, match="身份不一致"):
        await executor(drifted, campaign, tmp_path / "unused", CancelToken())

    assert not (tmp_path / "unused").exists()


def test_completed_trial_rejects_evidence_copied_from_another_run() -> None:
    run_id = uuid4()
    trial = evidence(run_id)
    current_task = task()
    report = trial.report.model_copy(
        update={
            "git": trial.report.git.model_copy(
                update={"baseline_revision": current_task.repository.source_revision}
            )
        }
    )
    state = trial.state.model_copy(
        update={
            "baseline_revision": current_task.repository.source_revision,
            "report_sha256": eval_report_sha256(report),
        }
    )
    case = CodingEvalTaskPackCase(
        case_id="fixed-run-identity",
        task_kind="bug_fix",
        repository_id="repository",
        profile_id="unit",
        task=current_task,
    )

    _require_completed_trial(state, report, trial.turn, case, state.environment, run_id)
    with pytest.raises(KernelError, match="运行状态、报告或Session不一致"):
        _require_completed_trial(
            state,
            report,
            trial.turn,
            case,
            state.environment,
            uuid4(),
        )


def test_missing_profile_calls_remain_empty_grader_evidence() -> None:
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    case = loaded.manifest.case("agents-dump-compatible-refactor")

    baseline, final = _profile_observations(completed_turn(), case)

    assert baseline == () and final == ()


def test_profile_call_without_trusted_process_terminal_fails_closed() -> None:
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    case = loaded.manifest.case("agents-dump-compatible-refactor")
    current = completed_turn()
    rewritten = []
    changed = False
    for item in current.items:
        content = item.content
        if not changed and isinstance(content, ToolCallContent) and content.tool == "run_tests":
            content = content.model_copy(update={"tool": f"run_profile.{case.profile_id}"})
            item = item.model_copy(update={"content": content})
            changed = True
        rewritten.append(item)

    with pytest.raises(KernelError, match="缺少可信终态"):
        _profile_observations(current.model_copy(update={"items": tuple(rewritten)}), case)


def test_completed_run_state_preserves_empty_baseline_evidence() -> None:
    payload = evidence(uuid4()).state.model_dump()
    payload["baseline_observations"] = ()

    state = CodingEvalRunState.model_validate(payload, strict=True)

    assert state.status == "completed" and state.baseline_observations == ()
