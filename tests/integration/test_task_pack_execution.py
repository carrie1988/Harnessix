from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
)
from harnessix.domain.models import ApprovalOutcome, utc_now
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.report import read_eval_campaign_execution_state, read_eval_report
from harnessix.evals.suite_contracts import CodingEvalSuiteCasePlan
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_execution import TaskPackCaseExecutor
from harnessix.evals.task_pack_publication import (
    TaskPackHistoryReadControl,
    open_task_pack_publication,
)
from harnessix.models.contracts import ToolCallCompleted
from harnessix.models.pricing import BillingContext, FlatInputPrice, PriceSnapshot
from scripts.recorded_task_pack import RecordedProviderFactory, RecordedSolutionProvider

_ROOT = Path(__file__).resolve().parents[2]
_SOLUTIONS = _ROOT / "benchmarks/taskpacks/harnessix-engineering-v1/solutions"
_SOLUTIONS_V2 = _ROOT / "benchmarks/taskpacks/harnessix-engineering-v2/solutions"
_MODEL = "harnessix-recorded-v1"
_CASE_ID = "agents-normalize-tool-name"


def _executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        pytest.skip(f"{name}不可用")
    return Path(value).resolve()


def _revision(git: Path) -> str:
    return subprocess.check_output(
        (str(git), "rev-parse", "HEAD"),
        cwd=_ROOT,
        text=True,
    ).strip()


def _fixed_plan(case, revision: str) -> tuple[CodingEvalSuiteCasePlan, CodingEvalCampaignPlan]:
    now = utc_now()
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
    billing = BillingContext(
        billing_provider="harnessix-recorded",
        region="offline",
        service_tier="eval",
        inference_mode="recorded",
    )
    environment = CodingEvalEnvironment(
        harnessix_revision=revision,
        provider="recorded",
        model=_MODEL,
        platform=sys.platform,
        isolation="fixed-container-no-network",
    )
    campaign = CodingEvalCampaignPlan(
        campaign_id=uuid4(),
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        environment=environment,
        run_ids=(uuid4(), uuid4()),
        price=price,
        billing_context=billing,
        created_at=now,
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
    return expected, campaign


@pytest.mark.skipif(os.name != "posix", reason="Task Pack Container Adapter要求POSIX宿主")
async def test_task_pack_case_commits_completed_campaign_progress(
    tmp_path: Path,
) -> None:
    docker = _executable("docker")
    git = _executable("git")
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    case = loaded.manifest.case(_CASE_ID)
    source_profile = loaded.manifest.profile(case.profile_id)
    image_environment = (
        "HARNESSIX_TEST_PYTHON_IMAGE"
        if source_profile.language == "python"
        else "HARNESSIX_TEST_NODE_IMAGE"
    )
    if os.environ.get(image_environment) != source_profile.image:
        pytest.skip("未配置Task Pack固定镜像")
    expected, campaign = _fixed_plan(case, _revision(git))
    provider_factory = RecordedProviderFactory(
        loaded,
        git_executable=git,
        oracle_root=tmp_path / "oracle",
        solutions_root=_SOLUTIONS,
    )
    provider_factory.prepare((_CASE_ID,))
    case_root = tmp_path / "case"
    case_root.mkdir(mode=0o700)
    executor = TaskPackCaseExecutor(loaded, git, docker, provider_factory)

    result = await executor(expected, campaign, case_root, CancelToken())
    state = read_eval_campaign_execution_state(case_root / "campaign-state.json")

    assert result.reason == "completed" and result.report is not None
    assert state.status == "completed"
    assert state.completed_run_ids == campaign.run_ids
    assert state.known_cost_amount == "0"
    assert state.report_sha256 is not None


@pytest.mark.skipif(os.name != "posix", reason="Task Pack Container Adapter要求POSIX宿主")
@pytest.mark.parametrize("omit_optional_selectors", (False, True))
async def test_task_pack_case_runs_two_trials_through_formal_agent_campaign_and_reopens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omit_optional_selectors: bool,
) -> None:
    docker = _executable("docker")
    git = _executable("git")
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    case = loaded.manifest.case(_CASE_ID)
    source_profile = loaded.manifest.profile(case.profile_id)
    image_environment = (
        "HARNESSIX_TEST_PYTHON_IMAGE"
        if source_profile.language == "python"
        else "HARNESSIX_TEST_NODE_IMAGE"
    )
    if os.environ.get(image_environment) != source_profile.image:
        pytest.skip("未配置Task Pack固定镜像")
    if omit_optional_selectors:
        original = RecordedSolutionProvider._step_event

        def omit_selectors(provider, response_id, step):
            event, finish = original(provider, response_id, step)
            if isinstance(event, ToolCallCompleted) and event.tool.startswith("run_profile."):
                event = event.model_copy(
                    update={"arguments": {"profile": event.arguments["profile"]}}
                )
            return event, finish

        monkeypatch.setattr(RecordedSolutionProvider, "_step_event", omit_selectors)
    expected, campaign = _fixed_plan(case, _revision(git))
    provider_factory = RecordedProviderFactory(
        loaded,
        git_executable=git,
        oracle_root=tmp_path / "oracle",
        solutions_root=_SOLUTIONS,
    )
    provider_factory.prepare((_CASE_ID,))

    case_root = tmp_path / "case"
    case_root.mkdir(mode=0o700)
    report_crashed = False
    campaign_crashed = False

    def fault(point: str) -> None:
        nonlocal report_crashed, campaign_crashed
        if point == "task_pack_runner.after_report" and not report_crashed:
            report_crashed = True
            raise RuntimeError("模拟Trial报告写入后宿主崩溃")
        if point == "task_pack_case.after_campaign_report" and not campaign_crashed:
            campaign_crashed = True
            raise RuntimeError("模拟Campaign报告写入后宿主崩溃")

    executor = TaskPackCaseExecutor(loaded, git, docker, provider_factory, fault=fault)

    with pytest.raises(RuntimeError, match="Trial报告写入后"):
        await executor(expected, campaign, case_root, CancelToken())

    with pytest.raises(RuntimeError, match="Campaign报告写入后"):
        await executor(expected, campaign, case_root, CancelToken())

    def forbidden(*_args):
        raise AssertionError("完整Case恢复不得创建Provider")

    recovered = TaskPackCaseExecutor(loaded, git, docker, forbidden)
    result = await recovered(expected, campaign, case_root, CancelToken())

    assert result.reason == "completed" and result.report is not None
    failed_checks = {
        str(run_id): tuple(
            check.code
            for check in read_eval_report(case_root / "runs" / str(run_id) / "report.json").checks
            if not check.passed
        )
        for run_id in campaign.run_ids
    }
    assert result.report.campaign.summary.passed_trials == 2, failed_checks
    assert result.report.campaign.summary.cost_completeness == "complete"
    assert result.report.campaign.summary.known_cost_amount == "0"
    assert provider_factory.opened_run_ids == set(campaign.run_ids)
    assert len(provider_factory.providers) == 2
    assert [len(provider.requests) for provider in provider_factory.providers] == [6, 6]
    assert all(item.automated_approval_decisions == 3 for item in result.report.transcripts)
    assert all(item.human_intervention_count == 0 for item in result.report.transcripts)
    assert all(item.outcome == "passed" for item in result.report.tests)
    for run_id in campaign.run_ids:
        root = case_root / "runs" / str(run_id)
        assert (root / "session.sqlite").is_file()
        assert (root / "action-audit.db").is_file()
        assert (root / "process-owner/process-leases.db").is_file()
        assert (root / "workspace-transactions/transactions.db").is_file()
        assert (root / "run-state.json").stat().st_mode & 0o777 == 0o600
        assert (root / "report.json").stat().st_mode & 0o777 == 0o600

    assert await recovered(expected, campaign, case_root, CancelToken()) == result


@pytest.mark.skipif(os.name != "posix", reason="Task Pack Container Adapter要求POSIX宿主")
async def test_task_pack_recovers_from_patch_before_failed_baseline_through_real_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实Runtime先持久拒绝越序Patch，再沿同一Turn取得失败基线并完成任务。"""

    docker = _executable("docker")
    git = _executable("git")
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    case = loaded.manifest.case(_CASE_ID)
    source_profile = loaded.manifest.profile(case.profile_id)
    image_environment = (
        "HARNESSIX_TEST_PYTHON_IMAGE"
        if source_profile.language == "python"
        else "HARNESSIX_TEST_NODE_IMAGE"
    )
    if os.environ.get(image_environment) != source_profile.image:
        pytest.skip("未配置Task Pack固定镜像")

    expected, campaign = _fixed_plan(case, _revision(git))
    patch_first_run_id = campaign.run_ids[0]
    original_step_event = RecordedSolutionProvider._step_event

    def patch_before_baseline(
        provider: RecordedSolutionProvider,
        response_id: str,
        step: int,
    ):
        if provider._run_id != patch_first_run_id:  # noqa: SLF001 - 固定测试Provider身份
            return original_step_event(provider, response_id, step)
        if step == 1:
            return (
                ToolCallCompleted(
                    call_id=f"{response_id}-patch-before-baseline",
                    tool="apply_patch_batch",
                    arguments=provider._patch(),  # noqa: SLF001 - 复用唯一Golden Patch构造
                ),
                "tool_calls",
            )
        return original_step_event(provider, response_id, step - 1)

    monkeypatch.setattr(RecordedSolutionProvider, "_step_event", patch_before_baseline)
    provider_factory = RecordedProviderFactory(
        loaded,
        git_executable=git,
        oracle_root=tmp_path / "oracle",
        solutions_root=_SOLUTIONS_V2,
    )
    provider_factory.prepare((_CASE_ID,))

    case_root = tmp_path / "case"
    case_root.mkdir(mode=0o700)
    rejected_approval_points = 0

    def fault(point: str) -> None:
        nonlocal rejected_approval_points
        if point != "task_pack_runner.after_rejected_approval":
            return
        rejected_approval_points += 1
        target = (
            case_root
            / "runs"
            / str(patch_first_run_id)
            / "workspace"
            / case.task.allowed_changed_paths[0]
        )
        expected_bytes = provider_factory.providers[-1]._solution.before  # noqa: SLF001
        assert expected_bytes is not None
        assert target.read_bytes() == expected_bytes

    executor = TaskPackCaseExecutor(loaded, git, docker, provider_factory, fault=fault)
    result = await executor(expected, campaign, case_root, CancelToken())

    assert result.reason == "completed" and result.report is not None
    assert rejected_approval_points == 1
    assert [len(provider.requests) for provider in provider_factory.providers] == [7, 6]
    assert [item.automated_approval_decisions for item in result.report.transcripts] == [4, 3]
    assert all(item.human_intervention_count == 0 for item in result.report.transcripts)
    assert all(item.outcome == "passed" for item in result.report.tests)

    run_root = case_root / "runs" / str(patch_first_run_id)
    report = read_eval_report(run_root / "report.json")
    baseline = [
        (item.check_id, item.passed, item.returncode) for item in report.baseline_observations
    ]
    final = [(item.check_id, item.passed, item.returncode) for item in report.final_observations]
    assert baseline == [(case.profile_id, False, 1)]
    assert final == [(case.profile_id, True, 0)]
    checks = {check.code: check.passed for check in report.checks}
    assert all(
        checks[code]
        for code in (
            "baseline_checks_failed",
            "behavior_checks_passed",
            "regression_checks_passed",
            "allowed_changes",
        )
    )

    async with open_task_pack_publication(run_root, existing_only=True) as owner:
        thread = await owner.authenticated_single_thread(
            TaskPackHistoryReadControl.begin(CancelToken())
        )
    assert thread is not None and len(thread.turns) == 1
    turn = thread.turns[0]
    approvals = tuple(
        item.content
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    )
    assert [approval.decision.outcome for approval in approvals if approval.decision] == [
        ApprovalOutcome.REJECTED,
        ApprovalOutcome.APPROVED,
        ApprovalOutcome.APPROVED,
        ApprovalOutcome.APPROVED,
    ]
    assert approvals[0].decision is not None
    assert approvals[0].decision.reason == "Task Pack要求修改前先运行固定Profile并观察失败基线"

    patch_calls = tuple(
        item.content
        for item in turn.items
        if isinstance(item.content, ToolCallContent)
        and item.content.tool == "apply_patch_batch"
    )
    results = {
        item.content.call_id: item.content
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
    }
    assert len(patch_calls) == 2
    rejected, applied = (results[call.call_id] for call in patch_calls)
    assert rejected.outcome == "failed"
    assert rejected.error is not None and rejected.error.code == "approval_rejected"
    assert rejected.patch is None and rejected.patch_batch is None
    assert rejected.trusted_action is not None
    assert rejected.trusted_action.state == "failed"
    assert rejected.trusted_action.artifact_sha256 is None
    assert applied.outcome == "succeeded" and applied.trusted_action is not None

    def forbidden(*_args):
        raise AssertionError("完整Case恢复不得创建Provider")

    recovered = TaskPackCaseExecutor(loaded, git, docker, forbidden)
    assert await recovered(expected, campaign, case_root, CancelToken()) == result
