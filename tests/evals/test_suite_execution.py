from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel, utc_now
from harnessix.evals.campaign_contracts import (
    CodingEvalCampaignPlan,
    summarize_campaign_trials,
)
from harnessix.evals.report import (
    read_eval_suite_case_report,
    read_eval_suite_execution_state,
    read_eval_suite_plan,
    read_eval_suite_report,
    write_eval_suite_case_report,
    write_eval_suite_execution_state,
)
from harnessix.evals.suite import build_coding_eval_suite_case_report
from harnessix.evals.suite_contracts import (
    CodingEvalSuiteCasePlan,
    CodingEvalSuiteCaseReport,
)
from harnessix.evals.suite_execution import run_coding_eval_suite
from harnessix.evals.suite_execution_contracts import (
    CodingEvalSuiteCaseRunResult,
    CodingEvalSuiteExecutionState,
    CodingEvalSuiteRunConfig,
    CodingEvalSuiteRunReport,
)
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.models.pricing import content_digest
from tests.evals.test_suite import _suite


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def create_private_root(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True)


def execution_fixture(
    tmp_path: Path,
    *,
    fee_stop_amount: str = "100",
) -> tuple[CodingEvalSuiteRunConfig, tuple[CodingEvalSuiteCaseReport, ...]]:
    plan, completed = _suite()
    reports = tuple(
        build_coding_eval_suite_case_report(expected, actual)
        for expected, actual in zip(plan.cases, completed, strict=True)
    )
    return (
        CodingEvalSuiteRunConfig(
            plan=plan,
            campaign_plans=tuple(case.campaign_plan for case in completed),
            work_root=str(tmp_path / "private-suite"),
            fee_stop_currency="USD",
            fee_stop_amount=fee_stop_amount,
        ),
        reports,
    )


class FixtureCaseExecutor:
    def __init__(
        self,
        reports: tuple[CodingEvalSuiteCaseReport, ...],
        *,
        cancel_case_id: str | None = None,
        stopped_case_id: str | None = None,
    ) -> None:
        self._reports = {report.case_id: report for report in reports}
        self.cancel_case_id = cancel_case_id
        self.stopped_case_id = stopped_case_id
        self.calls: list[str] = []

    async def __call__(
        self,
        case: CodingEvalSuiteCasePlan,
        campaign: CodingEvalCampaignPlan,
        case_root: Path,
        cancel: CancelToken,
    ) -> CodingEvalSuiteCaseRunResult:
        self.calls.append(case.case_id)
        assert case_root.name.startswith("case-")
        assert mode(case_root) == 0o700
        assert campaign.fingerprint == case.campaign_plan_fingerprint
        if case.case_id == self.cancel_case_id:
            cancel.cancel()
            cancel.checkpoint()
        if case.case_id == self.stopped_case_id:
            return CodingEvalSuiteCaseRunResult(
                case_id=case.case_id,
                reason="evidence_missing",
            )
        return CodingEvalSuiteCaseRunResult(
            case_id=case.case_id,
            reason="completed",
            report=self._reports[case.case_id],
        )


async def test_suite_runs_fixed_order_persists_report_and_reopens_without_case_replay(
    tmp_path: Path,
) -> None:
    config, reports = execution_fixture(tmp_path)
    executor = FixtureCaseExecutor(reports)

    result = await run_coding_eval_suite(config, executor)

    root = Path(config.work_root)
    state = read_eval_suite_execution_state(root / "suite-state.json")
    report = read_eval_suite_report(root / "suite-report.json")
    expected_ids = [case.case_id for case in config.plan.cases]
    assert executor.calls == expected_ids
    assert result.reason == "completed" and result.report_published
    assert state.status == "completed" and list(state.completed_case_ids) == expected_ids
    assert report.cases == reports
    assert read_eval_suite_plan(root / "suite-plan.json") == config.plan
    assert mode(root) == 0o700
    for name in ("suite-plan.json", "suite-state.json", "suite-report.json"):
        assert mode(root / name) == 0o600

    async def forbidden(*_):
        pytest.fail("已完成Suite不得再次执行Case")

    reopened = await run_coding_eval_suite(config, forbidden)
    assert reopened == result


async def test_suite_recovery_binds_provider_and_host_configuration(tmp_path: Path) -> None:
    config, reports = execution_fixture(tmp_path)
    binding = "a" * 64
    await run_coding_eval_suite(
        config,
        FixtureCaseExecutor(reports),
        execution_binding_sha256=binding,
    )

    async def forbidden(*_):
        pytest.fail("执行绑定漂移不得执行Case")

    with pytest.raises(KernelError, match="执行状态与固定配置不一致"):
        await run_coding_eval_suite(
            config,
            forbidden,
            execution_binding_sha256="b" * 64,
        )
    with pytest.raises(KernelError, match="执行绑定摘要无效"):
        await run_coding_eval_suite(config, forbidden, execution_binding_sha256="invalid")


async def test_suite_plan_is_durable_before_first_case_and_plan_crash_is_recoverable(
    tmp_path: Path,
) -> None:
    config, reports = execution_fixture(tmp_path)
    calls = 0

    def fault(point: str) -> None:
        if point == "suite.after_plan":
            raise RuntimeError("simulated exit after suite plan")

    with pytest.raises(RuntimeError, match="after suite plan"):
        await run_coding_eval_suite(config, FixtureCaseExecutor(reports), fault=fault)
    root = Path(config.work_root)
    assert read_eval_suite_plan(root / "suite-plan.json") == config.plan
    assert not (root / "suite-state.json").exists()

    async def checking_executor(case, campaign, case_root, cancel):
        nonlocal calls
        calls += 1
        assert read_eval_suite_plan(root / "suite-plan.json") == config.plan
        assert read_eval_suite_execution_state(root / "suite-state.json").status == "running"
        return CodingEvalSuiteCaseRunResult(
            case_id=case.case_id,
            reason="completed",
            report=reports[calls - 1],
        )

    result = await run_coding_eval_suite(config, checking_executor)
    assert result.reason == "completed" and calls == len(reports)


async def test_case_evidence_crash_advances_prefix_without_replaying_completed_case(
    tmp_path: Path,
) -> None:
    config, reports = execution_fixture(tmp_path)
    first = FixtureCaseExecutor(reports)
    crashed = False

    def fault(point: str) -> None:
        nonlocal crashed
        if point == "suite.after_case_evidence" and not crashed:
            crashed = True
            raise RuntimeError("simulated exit after case evidence")

    with pytest.raises(RuntimeError, match="after case evidence"):
        await run_coding_eval_suite(config, first, fault=fault)
    assert first.calls == [config.plan.cases[0].case_id]

    resumed = FixtureCaseExecutor(reports)
    result = await run_coding_eval_suite(config, resumed)
    assert result.reason == "completed"
    assert resumed.calls == [case.case_id for case in config.plan.cases[1:]]


async def test_executor_crash_reenters_same_case_identity(tmp_path: Path) -> None:
    config, reports = execution_fixture(tmp_path)
    crashed_calls: list[str] = []

    async def crash(case, campaign, case_root, cancel):
        crashed_calls.append(case.case_id)
        raise RuntimeError("simulated case host crash")

    with pytest.raises(RuntimeError, match="case host crash"):
        await run_coding_eval_suite(config, crash)
    state = read_eval_suite_execution_state(Path(config.work_root) / "suite-state.json")
    assert state.status == "running"
    assert state.current_case_id == config.plan.cases[0].case_id
    assert crashed_calls == [config.plan.cases[0].case_id]

    resumed = FixtureCaseExecutor(reports)
    assert (await run_coding_eval_suite(config, resumed)).reason == "completed"
    assert resumed.calls == [case.case_id for case in config.plan.cases]


async def test_report_publication_crash_recovers_without_case_executor(tmp_path: Path) -> None:
    config, reports = execution_fixture(tmp_path)

    def fault(point: str) -> None:
        if point == "suite.after_report":
            raise RuntimeError("simulated exit after suite report")

    with pytest.raises(RuntimeError, match="after suite report"):
        await run_coding_eval_suite(config, FixtureCaseExecutor(reports), fault=fault)
    root = Path(config.work_root)
    assert (root / "suite-report.json").exists()
    assert read_eval_suite_execution_state(root / "suite-state.json").status == "running"

    async def forbidden(*_):
        pytest.fail("Suite报告恢复不得再次执行Case")

    recovered = await run_coding_eval_suite(config, forbidden)
    assert recovered.reason == "completed" and recovered.report_published


async def test_cancel_requires_explicit_resume_and_preserves_completed_prefix(
    tmp_path: Path,
) -> None:
    config, reports = execution_fixture(tmp_path)
    second_id = config.plan.cases[1].case_id
    cancelled = FixtureCaseExecutor(reports, cancel_case_id=second_id)

    result = await run_coding_eval_suite(config, cancelled)
    assert result.reason == "cancelled"
    assert result.completed_cases == 1 and result.current_case_id == second_id

    async def forbidden(*_):
        pytest.fail("停止状态未经显式恢复不得执行Case")

    reopened = await run_coding_eval_suite(config, forbidden)
    assert reopened == result

    resumed = FixtureCaseExecutor(reports)
    completed = await run_coding_eval_suite(config, resumed, resume=True)
    assert completed.reason == "completed"
    assert resumed.calls == [case.case_id for case in config.plan.cases[1:]]


async def test_reported_evidence_stop_requires_explicit_resume(tmp_path: Path) -> None:
    config, reports = execution_fixture(tmp_path)
    first_id = config.plan.cases[0].case_id
    stopped = FixtureCaseExecutor(reports, stopped_case_id=first_id)

    result = await run_coding_eval_suite(config, stopped)
    assert result.reason == "evidence_missing" and result.completed_cases == 0
    assert (await run_coding_eval_suite(config, stopped)).reason == "evidence_missing"
    assert stopped.calls == [first_id]

    resumed = FixtureCaseExecutor(reports)
    assert (await run_coding_eval_suite(config, resumed, resume=True)).reason == "completed"


def incomplete_cost_report(report: CodingEvalSuiteCaseReport) -> CodingEvalSuiteCaseReport:
    first = report.campaign.trials[0].model_copy(
        update={
            "cost_completeness": "unknown",
            "known_cost_currency": None,
            "known_cost_amount": None,
        }
    )
    trials = (first, *report.campaign.trials[1:])
    campaign = report.campaign.model_copy(
        update={
            "trials": trials,
            "summary": summarize_campaign_trials(trials, report.campaign.plan.price.currency),
        }
    )
    changed = report.model_copy(
        update={
            "campaign": campaign,
            "campaign_report_fingerprint": content_digest(campaign),
        }
    )
    return CodingEvalSuiteCaseReport.model_validate_json(changed.model_dump_json(), strict=True)


async def test_unknown_cost_and_aggregate_fee_limit_stop_before_next_case(
    tmp_path: Path,
) -> None:
    unknown_config, reports = execution_fixture(tmp_path / "unknown")
    unknown_reports = (incomplete_cost_report(reports[0]), *reports[1:])
    unknown_executor = FixtureCaseExecutor(unknown_reports)

    unknown = await run_coding_eval_suite(unknown_config, unknown_executor)
    assert unknown.reason == "cost_unknown" and unknown.completed_cases == 1
    assert unknown_executor.calls == [unknown_config.plan.cases[0].case_id]

    fee_config, fee_reports = execution_fixture(
        tmp_path / "fee",
        fee_stop_amount="0.000000000000000001",
    )
    fee_executor = FixtureCaseExecutor(fee_reports)
    limited = await run_coding_eval_suite(fee_config, fee_executor)
    assert limited.reason == "fee_limit_reached" and limited.completed_cases == 1
    assert fee_executor.calls == [fee_config.plan.cases[0].case_id]


async def test_suite_rejects_wrong_case_missing_evidence_and_lock_contention(
    tmp_path: Path,
) -> None:
    config, reports = execution_fixture(tmp_path / "identity")

    async def wrong_case(*_):
        return CodingEvalSuiteCaseRunResult(
            case_id=reports[1].case_id,
            reason="completed",
            report=reports[1],
        )

    with pytest.raises(KernelError) as mismatch:
        await run_coding_eval_suite(config, wrong_case)
    assert mismatch.value.code == "eval_suite_case_mismatch"

    complete_config, complete_reports = execution_fixture(tmp_path / "missing")
    await run_coding_eval_suite(complete_config, FixtureCaseExecutor(complete_reports))
    first_report = Path(complete_config.work_root) / "cases" / "case-000" / "case-report.json"
    first_report.unlink()
    with pytest.raises(KernelError) as missing:
        await run_coding_eval_suite(complete_config, FixtureCaseExecutor(complete_reports))
    assert missing.value.code == "eval_suite_evidence_order_invalid"

    lock_config, lock_reports = execution_fixture(tmp_path / "lock")
    root = Path(lock_config.work_root)
    create_private_root(root)
    descriptor = os.open(root / ".suite.lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquire_exclusive_file_lock(descriptor)
    try:
        with pytest.raises(KernelError) as busy:
            await run_coding_eval_suite(lock_config, FixtureCaseExecutor(lock_reports))
        assert busy.value.code == "eval_suite_busy"
    finally:
        os.close(descriptor)


def test_suite_execution_config_rejects_campaign_and_run_identity_drift(tmp_path: Path) -> None:
    config, _ = execution_fixture(tmp_path)
    body = config.model_dump(mode="json")
    body["campaign_plans"][0]["task_id"] = "other-task"
    with pytest.raises(ValidationError, match="身份不一致"):
        CodingEvalSuiteRunConfig.model_validate_json(json.dumps(body), strict=True)

    body = config.model_dump(mode="json")
    body["campaign_plans"][1]["run_ids"][0] = body["campaign_plans"][0]["run_ids"][0]
    body["plan"]["cases"][1]["campaign_plan_fingerprint"] = content_digest(
        CodingEvalCampaignPlan.model_validate_json(
            json.dumps(body["campaign_plans"][1]), strict=True
        )
    )
    with pytest.raises(ValidationError, match="Run ID必须唯一"):
        CodingEvalSuiteRunConfig.model_validate_json(json.dumps(body), strict=True)


def test_suite_case_report_and_state_io_are_private_atomic_and_no_follow(
    tmp_path: Path,
) -> None:
    config, reports = execution_fixture(tmp_path)
    root = tmp_path / "io"
    root.mkdir(mode=0o700)
    now = utc_now()
    state = CodingEvalSuiteExecutionState(
        suite_id=config.plan.suite_id,
        plan_fingerprint=config.plan.fingerprint,
        execution_config_fingerprint=config.fingerprint,
        status="ready",
        completed_case_ids=(),
        known_cost_currency=config.fee_stop_currency,
        known_cost_amount="0",
        started_at=now,
        updated_at=now,
    )
    state_path = root / "suite-state.json"
    case_path = root / "case-report.json"
    write_eval_suite_execution_state(state_path, state)
    write_eval_suite_case_report(case_path, reports[0])
    assert read_eval_suite_execution_state(state_path) == state
    assert read_eval_suite_case_report(case_path) == reports[0]
    assert mode(state_path) == mode(case_path) == 0o600

    case_path.chmod(0o644)
    with pytest.raises(KernelError) as permissions:
        read_eval_suite_case_report(case_path)
    assert permissions.value.code == "eval_suite_case_report_invalid"

    target = root / "target.json"
    target.write_text("{}", encoding="utf-8")
    state_path.unlink()
    state_path.symlink_to(target)
    with pytest.raises(KernelError) as link:
        write_eval_suite_execution_state(state_path, state)
    assert link.value.code == "eval_suite_execution_state_path_denied"


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("coding-eval-suite-run-config", CodingEvalSuiteRunConfig),
        ("coding-eval-suite-case-run-result", CodingEvalSuiteCaseRunResult),
        ("coding-eval-suite-execution-state", CodingEvalSuiteExecutionState),
        ("coding-eval-suite-run-report", CodingEvalSuiteRunReport),
    ],
)
def test_suite_execution_public_schema_is_frozen(name: str, model: type[ContractModel]) -> None:
    expected = json.loads(Path(f"spec/{name}-v1.schema.json").read_text(encoding="utf-8"))
    assert expected == model.model_json_schema()
