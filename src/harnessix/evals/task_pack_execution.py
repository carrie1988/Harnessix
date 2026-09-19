"""Task Pack Case到现有Campaign和Suite端口的可恢复适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.evals.campaign import CompletedCodingEvalTrial, build_coding_eval_campaign_report
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.campaign_execution_contracts import CodingEvalCampaignExecutionState
from harnessix.evals.execution_fs import (
    ensure_private_directory,
    exclusive_execution_lock,
    path_present,
)
from harnessix.evals.report import (
    eval_campaign_report_sha256,
    read_eval_campaign_execution_state,
    read_eval_campaign_plan,
    read_eval_campaign_report,
    write_eval_campaign_execution_state,
    write_eval_campaign_plan,
    write_eval_campaign_report,
)
from harnessix.evals.suite import CompletedCodingEvalSuiteCase, build_coding_eval_suite_case_report
from harnessix.evals.suite_contracts import CodingEvalSuiteCasePlan
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteCaseRunResult
from harnessix.evals.task_pack import LoadedCodingEvalTaskPack
from harnessix.evals.task_pack_contracts import CodingEvalTaskPackCase
from harnessix.evals.task_pack_trial import (
    Fault,
    TaskPackProviderFactory,
    _load_completed_run,
    run_task_pack_coding_eval,
)
from harnessix.models.costs import bind_price, build_cost_report
from harnessix.models.pricing import amount_units, format_amount
from harnessix.observability import Observability
from harnessix.tools.workspace import digest

_PLAN_FILE = "campaign-plan.json"
_STATE_FILE = "campaign-state.json"
_REPORT_FILE = "campaign-report.json"
_RUNS_DIRECTORY = "runs"
_RUN_STATE_FILE = "run-state.json"
_RUN_REPORT_FILE = "report.json"
_LOCK_FILE = ".task-pack-case.lock"


def _fault(_: str) -> None:
    return None


def _execution_fingerprint(
    loaded: LoadedCodingEvalTaskPack,
    case: CodingEvalTaskPackCase,
    campaign: CodingEvalCampaignPlan,
) -> str:
    profile = loaded.manifest.profile(case.profile_id)
    return digest(
        {
            "spec_version": "harnessix.task-pack-case-execution-binding/v1",
            "pack_id": loaded.manifest.pack_id,
            "pack_version": loaded.manifest.pack_version,
            "pack_sha256": loaded.manifest.pack_sha256,
            "case_id": case.case_id,
            "task_fingerprint": case.task.fingerprint,
            "profile_sha256": profile.profile_sha256,
            "campaign_plan_fingerprint": campaign.fingerprint,
        }
    )


def _require_case_scope(
    loaded: LoadedCodingEvalTaskPack,
    expected: CodingEvalSuiteCasePlan,
    campaign: CodingEvalCampaignPlan,
) -> CodingEvalTaskPackCase:
    try:
        case = loaded.manifest.case(expected.case_id)
    except KeyError:
        raise KernelError(
            "eval_task_pack_case_not_found", "Suite引用的Task Pack Case不存在"
        ) from None
    task = case.task
    if (
        expected.task_kind != case.task_kind
        or expected.task_id != task.task_id
        or expected.task_version != task.task_version
        or expected.task_fingerprint != task.fingerprint
        or expected.repository != task.repository
        or expected.campaign_plan_fingerprint != campaign.fingerprint
        or campaign.task_id != task.task_id
        or campaign.task_version != task.task_version
        or campaign.task_fingerprint != task.fingerprint
    ):
        raise KernelError("eval_suite_case_mismatch", "Task Pack Case、Suite与Campaign身份不一致")
    return case


async def _completed_trial(
    case_root: Path,
    case: CodingEvalTaskPackCase,
    campaign: CodingEvalCampaignPlan,
    run_id: UUID,
) -> CompletedCodingEvalTrial:
    run = await _load_completed_run(
        case_root / _RUNS_DIRECTORY / str(run_id),
        case,
        campaign.environment,
        run_id,
    )
    try:
        bindings = tuple(
            bind_price(attempt, campaign.price, campaign.billing_context)
            for attempt in run.turn.accounted_attempts
        )
        cost = build_cost_report(run.turn, bindings)
    except ValueError:
        raise KernelError("eval_campaign_cost_invalid", "Task Pack Trial成本无法安全重算") from None
    return CompletedCodingEvalTrial(run.state, run.report, run.turn, cost)


def _known_cost(trial: CompletedCodingEvalTrial, currency: str) -> tuple[int, bool]:
    totals = trial.cost.summary.totals
    if len(totals) > 1 or (totals and totals[0].currency != currency):
        raise KernelError("eval_campaign_cost_invalid", "Task Pack Trial成本币种不一致")
    units = amount_units(totals[0].known_amount) if totals else 0
    return units, trial.cost.summary.completeness == "complete"


@dataclass(frozen=True, slots=True)
class _CaseExecution:
    loaded: LoadedCodingEvalTaskPack
    git_executable: Path
    container_engine: Path
    provider_factory: TaskPackProviderFactory
    observability: Observability | None
    fault: Fault
    expected: CodingEvalSuiteCasePlan
    campaign: CodingEvalCampaignPlan
    root: Path
    case: CodingEvalTaskPackCase

    @property
    def state_path(self) -> Path:
        return self.root / _STATE_FILE


def _require_plan(context: _CaseExecution) -> None:
    path = context.root / _PLAN_FILE
    if path_present(path):
        if read_eval_campaign_plan(path) != context.campaign:
            raise KernelError("eval_campaign_plan_mismatch", "Task Pack Campaign计划漂移")
    else:
        write_eval_campaign_plan(path, context.campaign)
    context.fault("task_pack_case.after_plan")


def _load_state(context: _CaseExecution) -> CodingEvalCampaignExecutionState:
    fingerprint = _execution_fingerprint(context.loaded, context.case, context.campaign)
    if path_present(context.state_path):
        state = read_eval_campaign_execution_state(context.state_path)
    else:
        now = utc_now()
        state = CodingEvalCampaignExecutionState(
            campaign_id=context.campaign.campaign_id,
            plan_fingerprint=context.campaign.fingerprint,
            execution_config_fingerprint=fingerprint,
            status="ready",
            completed_run_ids=(),
            known_cost_currency=context.campaign.price.currency,
            known_cost_amount="0",
            started_at=now,
            updated_at=now,
        )
        write_eval_campaign_execution_state(context.state_path, state)
    if (
        state.campaign_id != context.campaign.campaign_id
        or state.plan_fingerprint != context.campaign.fingerprint
        or state.execution_config_fingerprint != fingerprint
        or state.completed_run_ids != context.campaign.run_ids[: len(state.completed_run_ids)]
        or state.known_cost_currency != context.campaign.price.currency
    ):
        raise KernelError("eval_campaign_execution_mismatch", "Task Pack Campaign状态漂移")
    return state


async def _load_prefix(
    context: _CaseExecution,
    state: CodingEvalCampaignExecutionState,
) -> tuple[CodingEvalCampaignExecutionState, list[CompletedCodingEvalTrial], int, bool]:
    completed: list[CompletedCodingEvalTrial] = []
    known_units = 0
    all_costs_complete = True
    gap = False
    for run_id in context.campaign.run_ids:
        run_root = context.root / _RUNS_DIRECTORY / str(run_id)
        present = path_present(run_root / _RUN_STATE_FILE) and path_present(
            run_root / _RUN_REPORT_FILE
        )
        if not present:
            gap = True
            continue
        if gap:
            raise KernelError(
                "eval_campaign_evidence_order_invalid", "Task Pack Trial证据不是连续前缀"
            )
        trial = await _completed_trial(context.root, context.case, context.campaign, run_id)
        units, complete = _known_cost(trial, context.campaign.price.currency)
        completed.append(trial)
        known_units += units
        all_costs_complete = all_costs_complete and complete
    if len(state.completed_run_ids) > len(completed):
        raise KernelError("eval_campaign_evidence_missing", "Task Pack Campaign已完成证据缺失")
    claimed_units = sum(
        _known_cost(trial, context.campaign.price.currency)[0]
        for trial in completed[: len(state.completed_run_ids)]
    )
    if format_amount(claimed_units) != state.known_cost_amount:
        raise KernelError("eval_campaign_cost_mismatch", "Task Pack Campaign成本前缀不一致")
    if len(completed) > len(state.completed_run_ids):
        state = state.model_copy(
            update={
                "completed_run_ids": context.campaign.run_ids[: len(completed)],
                "known_cost_amount": format_amount(known_units),
                "updated_at": utc_now(),
            }
        )
        write_eval_campaign_execution_state(context.state_path, state)
    return state, completed, known_units, all_costs_complete


def _case_result(
    context: _CaseExecution,
    completed: list[CompletedCodingEvalTrial],
) -> CodingEvalSuiteCaseRunResult:
    report = build_coding_eval_suite_case_report(
        context.expected,
        CompletedCodingEvalSuiteCase(
            case_id=context.case.case_id,
            task=context.case.task,
            campaign_plan=context.campaign,
            trials=tuple(completed),
        ),
    )
    return CodingEvalSuiteCaseRunResult(
        case_id=context.case.case_id,
        reason="completed",
        report=report,
    )


def _recover_campaign_report(
    context: _CaseExecution,
    state: CodingEvalCampaignExecutionState,
    completed: list[CompletedCodingEvalTrial],
) -> CodingEvalSuiteCaseRunResult | None:
    path = context.root / _REPORT_FILE
    if not path_present(path):
        if state.status == "completed":
            raise KernelError("eval_campaign_report_mismatch", "Task Pack Campaign完成状态缺少报告")
        return None
    if len(completed) != len(context.campaign.run_ids):
        raise KernelError("eval_campaign_report_mismatch", "Task Pack Campaign报告早于完整证据")
    report = read_eval_campaign_report(path)
    expected = build_coding_eval_campaign_report(context.campaign, tuple(completed))
    if report != expected:
        raise KernelError("eval_campaign_report_mismatch", "Task Pack Campaign报告不一致")
    digest = eval_campaign_report_sha256(report)
    if state.status != "completed":
        state = state.model_copy(
            update={"status": "completed", "report_sha256": digest, "updated_at": utc_now()}
        )
        write_eval_campaign_execution_state(context.state_path, state)
    elif state.report_sha256 != digest:
        raise KernelError("eval_campaign_report_mismatch", "Task Pack Campaign摘要不一致")
    return _case_result(context, completed)


def _stop_for_unknown_cost(
    context: _CaseExecution,
    state: CodingEvalCampaignExecutionState,
) -> CodingEvalSuiteCaseRunResult:
    if state.status != "stopped":
        state = state.model_copy(
            update={"status": "stopped", "stop_reason": "cost_unknown", "updated_at": utc_now()}
        )
        write_eval_campaign_execution_state(context.state_path, state)
    return CodingEvalSuiteCaseRunResult(case_id=context.case.case_id, reason="cost_unknown")


async def _execute_remaining(
    context: _CaseExecution,
    state: CodingEvalCampaignExecutionState,
    completed: list[CompletedCodingEvalTrial],
    known_units: int,
    cancel: CancelToken,
) -> CodingEvalSuiteCaseRunResult | None:
    for run_id in context.campaign.run_ids[len(completed) :]:
        cancel.checkpoint()
        state = state.model_copy(update={"status": "running", "updated_at": utc_now()})
        write_eval_campaign_execution_state(context.state_path, state)
        await run_task_pack_coding_eval(
            context.loaded,
            context.root / _RUNS_DIRECTORY,
            context.git_executable,
            context.container_engine,
            context.case.case_id,
            run_id,
            context.provider_factory,
            context.campaign.environment,
            cancel,
            observability=context.observability,
            fault=context.fault,
        )
        trial = await _completed_trial(context.root, context.case, context.campaign, run_id)
        units, complete = _known_cost(trial, context.campaign.price.currency)
        completed.append(trial)
        known_units += units
        state = state.model_copy(
            update={
                "completed_run_ids": context.campaign.run_ids[: len(completed)],
                "known_cost_amount": format_amount(known_units),
                "updated_at": utc_now(),
            }
        )
        write_eval_campaign_execution_state(context.state_path, state)
        context.fault("task_pack_case.after_trial")
        if not complete:
            return _stop_for_unknown_cost(context, state)
    return None


def _publish_campaign(
    context: _CaseExecution,
    state: CodingEvalCampaignExecutionState,
    completed: list[CompletedCodingEvalTrial],
) -> CodingEvalSuiteCaseRunResult:
    report = build_coding_eval_campaign_report(context.campaign, tuple(completed))
    write_eval_campaign_report(context.root / _REPORT_FILE, report)
    context.fault("task_pack_case.after_campaign_report")
    state = state.model_copy(
        update={
            "status": "completed",
            "report_sha256": eval_campaign_report_sha256(report),
            "updated_at": utc_now(),
        }
    )
    write_eval_campaign_execution_state(context.state_path, state)
    return _case_result(context, completed)


async def _run_locked_case(
    context: _CaseExecution,
    cancel: CancelToken,
) -> CodingEvalSuiteCaseRunResult:
    _require_plan(context)
    state = _load_state(context)
    state, completed, known_units, costs_complete = await _load_prefix(context, state)
    recovered = _recover_campaign_report(context, state, completed)
    if recovered is not None:
        return recovered
    if state.status == "stopped" or not costs_complete:
        return _stop_for_unknown_cost(context, state)
    stopped = await _execute_remaining(context, state, completed, known_units, cancel)
    return stopped or _publish_campaign(context, state, completed)


@dataclass(slots=True)
class TaskPackCaseExecutor:
    """Suite Case端口：以固定Run ID复用正式Agent和Campaign证据合同。"""

    loaded: LoadedCodingEvalTaskPack
    git_executable: Path
    container_engine: Path
    provider_factory: TaskPackProviderFactory
    observability: Observability | None = None
    fault: Fault = _fault

    async def __call__(
        self,
        expected: CodingEvalSuiteCasePlan,
        campaign: CodingEvalCampaignPlan,
        case_root: Path,
        cancel: CancelToken,
    ) -> CodingEvalSuiteCaseRunResult:
        case = _require_case_scope(self.loaded, expected, campaign)
        ensure_private_directory(
            case_root,
            error_code="eval_task_pack_case_root_invalid",
            label="Task Pack Case目录",
        )
        ensure_private_directory(
            case_root / _RUNS_DIRECTORY,
            error_code="eval_task_pack_case_root_invalid",
            label="Task Pack Run目录",
        )
        with exclusive_execution_lock(
            case_root,
            _LOCK_FILE,
            busy_code="eval_task_pack_case_busy",
            invalid_code="eval_task_pack_case_lock_invalid",
            label="Task Pack Case",
        ):
            context = _CaseExecution(
                self.loaded,
                self.git_executable,
                self.container_engine,
                self.provider_factory,
                self.observability,
                self.fault,
                expected,
                campaign,
                case_root,
                case,
            )
            return await _run_locked_case(context, cancel)
