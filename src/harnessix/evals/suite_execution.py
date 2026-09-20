"""顺序、单写者且可从Case证据恢复的Coding Eval Suite执行器。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import Field

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel, utc_now
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.execution_fs import (
    ensure_private_directory,
    exclusive_execution_lock,
    path_present,
)
from harnessix.evals.report import (
    eval_suite_report_sha256,
    read_eval_suite_case_report,
    read_eval_suite_execution_state,
    read_eval_suite_plan,
    read_eval_suite_report,
    write_eval_suite_case_report,
    write_eval_suite_execution_state,
    write_eval_suite_plan,
    write_eval_suite_report,
)
from harnessix.evals.suite import build_coding_eval_suite_report_from_cases
from harnessix.evals.suite_contracts import (
    CodingEvalSuiteCasePlan,
    CodingEvalSuiteCaseReport,
)
from harnessix.evals.suite_execution_contracts import (
    CodingEvalSuiteCaseRunResult,
    CodingEvalSuiteExecutionState,
    CodingEvalSuiteRunConfig,
    CodingEvalSuiteRunReport,
    SuiteRunReason,
    SuiteStopReason,
)
from harnessix.models.pricing import amount_units, content_digest, format_amount

SuiteCaseExecutor = Callable[
    [CodingEvalSuiteCasePlan, CodingEvalCampaignPlan, Path, CancelToken],
    Awaitable[CodingEvalSuiteCaseRunResult],
]
Fault = Callable[[str], None]

_PLAN_FILE = "suite-plan.json"
_STATE_FILE = "suite-state.json"
_REPORT_FILE = "suite-report.json"
_CASES_DIRECTORY = "cases"
_CASE_REPORT_FILE = "case-report.json"
_LOCK_FILE = ".suite.lock"


def _fault(_: str) -> None:
    return None


def _case_root(root: Path, index: int) -> Path:
    return root / _CASES_DIRECTORY / f"case-{index:03d}"


def _require_plan(path: Path, config: CodingEvalSuiteRunConfig) -> None:
    if path_present(path):
        if read_eval_suite_plan(path) != config.plan:
            raise KernelError("eval_suite_plan_mismatch", "已发布Suite计划与配置不一致")
        return
    write_eval_suite_plan(path, config.plan)


def suite_execution_fingerprint(
    config: CodingEvalSuiteRunConfig,
    execution_binding_sha256: str | None,
) -> str:
    """计算Suite配置与可选宿主绑定共同形成的恢复身份。"""

    if execution_binding_sha256 is None:
        return config.fingerprint
    return content_digest(
        _SuiteExecutionBinding(
            suite_config_sha256=config.fingerprint,
            host_binding_sha256=execution_binding_sha256,
        )
    )


class _SuiteExecutionBinding(ContractModel):
    """把Suite计划配置与宿主/Provider配置摘要绑定为恢复身份。"""

    spec_version: Literal["harnessix.coding-eval-suite-execution-binding/v1"] = (
        "harnessix.coding-eval-suite-execution-binding/v1"
    )
    suite_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _initial_state(
    config: CodingEvalSuiteRunConfig,
    execution_binding_sha256: str | None,
) -> CodingEvalSuiteExecutionState:
    now = utc_now()
    return CodingEvalSuiteExecutionState(
        suite_id=config.plan.suite_id,
        plan_fingerprint=config.plan.fingerprint,
        execution_config_fingerprint=suite_execution_fingerprint(config, execution_binding_sha256),
        status="ready",
        completed_case_ids=(),
        known_cost_currency=config.fee_stop_currency,
        known_cost_amount="0",
        started_at=now,
        updated_at=now,
    )


def _require_state(
    state: CodingEvalSuiteExecutionState,
    config: CodingEvalSuiteRunConfig,
    execution_binding_sha256: str | None,
) -> None:
    case_ids = tuple(case.case_id for case in config.plan.cases)
    completed = state.completed_case_ids
    next_case = case_ids[len(completed)] if len(completed) < len(case_ids) else None
    if (
        state.suite_id != config.plan.suite_id
        or state.plan_fingerprint != config.plan.fingerprint
        or state.execution_config_fingerprint
        != suite_execution_fingerprint(config, execution_binding_sha256)
        or completed != case_ids[: len(completed)]
        or state.known_cost_currency != config.fee_stop_currency
        or state.current_case_id not in {None, next_case}
        or (state.status == "completed" and len(completed) != len(case_ids))
    ):
        raise KernelError("eval_suite_execution_mismatch", "Suite执行状态与固定配置不一致")


def _require_case_report(
    report: CodingEvalSuiteCaseReport,
    expected: CodingEvalSuiteCasePlan,
    config: CodingEvalSuiteRunConfig,
    index: int,
) -> None:
    campaign = config.campaign_plans[index]
    if (
        report.case_id != expected.case_id
        or report.campaign.plan != campaign
        or report.campaign.plan_fingerprint != expected.campaign_plan_fingerprint
    ):
        raise KernelError("eval_suite_case_mismatch", "Suite Case报告与固定计划不一致")


def _case_cost(report: CodingEvalSuiteCaseReport, currency: str) -> tuple[int, bool]:
    summary = report.campaign.summary
    if summary.known_cost_currency not in {None, currency}:
        raise KernelError("eval_suite_cost_invalid", "Suite Case成本币种与固定停止线不一致")
    units = amount_units(summary.known_cost_amount) if summary.known_cost_amount is not None else 0
    return units, summary.cost_completeness == "complete"


def _prefix_cost(reports: list[CodingEvalSuiteCaseReport], currency: str) -> tuple[int, bool]:
    total = 0
    complete = True
    for report in reports:
        case_units, case_complete = _case_cost(report, currency)
        total += case_units
        complete = complete and case_complete
    return total, complete


def _load_contiguous_reports(
    root: Path,
    config: CodingEvalSuiteRunConfig,
) -> list[CodingEvalSuiteCaseReport]:
    reports: list[CodingEvalSuiteCaseReport] = []
    gap_seen = False
    for index, expected in enumerate(config.plan.cases):
        report_path = _case_root(root, index) / _CASE_REPORT_FILE
        if not path_present(report_path):
            gap_seen = True
            continue
        if gap_seen:
            raise KernelError("eval_suite_evidence_order_invalid", "Suite Case证据不是连续前缀")
        report = read_eval_suite_case_report(report_path)
        _require_case_report(report, expected, config, index)
        reports.append(report)
    return reports


def _reconcile_prefix(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    state: CodingEvalSuiteExecutionState,
) -> tuple[CodingEvalSuiteExecutionState, list[CodingEvalSuiteCaseReport], int, bool]:
    reports = _load_contiguous_reports(root, config)
    claimed_count = len(state.completed_case_ids)
    if claimed_count > len(reports):
        raise KernelError("eval_suite_evidence_missing", "Suite状态声明的Case证据缺失")
    claimed_units, _ = _prefix_cost(reports[:claimed_count], config.fee_stop_currency)
    if format_amount(claimed_units) != state.known_cost_amount:
        raise KernelError("eval_suite_cost_mismatch", "Suite已知成本与完成前缀不一致")
    if state.status == "ready" and reports:
        raise KernelError("eval_suite_execution_mismatch", "Suite ready状态出现Case证据")
    known_units, all_complete = _prefix_cost(reports, config.fee_stop_currency)
    if len(reports) > claimed_count:
        state = state.model_copy(
            update={
                "completed_case_ids": tuple(
                    case.case_id for case in config.plan.cases[: len(reports)]
                ),
                "current_case_id": None,
                "known_cost_amount": format_amount(known_units),
                "updated_at": utc_now(),
            }
        )
        write_eval_suite_execution_state(root / _STATE_FILE, state)
    return state, reports, known_units, all_complete


def _run_report(
    config: CodingEvalSuiteRunConfig,
    state: CodingEvalSuiteExecutionState,
    reason: SuiteRunReason,
    *,
    published: bool = False,
) -> CodingEvalSuiteRunReport:
    return CodingEvalSuiteRunReport.model_validate(
        {
            "reason": reason,
            "suite_id": config.plan.suite_id,
            "scheduled_cases": len(config.plan.cases),
            "completed_cases": len(state.completed_case_ids),
            "current_case_id": state.current_case_id,
            "report_published": published,
            "known_cost_currency": state.known_cost_currency,
            "known_cost_amount": state.known_cost_amount,
        }
    )


def _stop(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    state: CodingEvalSuiteExecutionState,
    reason: SuiteStopReason,
    *,
    current_case_id: str | None,
) -> CodingEvalSuiteRunReport:
    state = state.model_copy(
        update={
            "status": "stopped",
            "current_case_id": current_case_id,
            "stop_reason": reason,
            "updated_at": utc_now(),
        }
    )
    write_eval_suite_execution_state(root / _STATE_FILE, state)
    return _run_report(config, state, reason)


def _recover_published_report(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    state: CodingEvalSuiteExecutionState,
    cases: list[CodingEvalSuiteCaseReport],
) -> CodingEvalSuiteRunReport | None:
    report_path = root / _REPORT_FILE
    if not path_present(report_path):
        return None
    if len(cases) != len(config.plan.cases):
        raise KernelError("eval_suite_report_mismatch", "Suite报告早于全部Case完成")
    expected = build_coding_eval_suite_report_from_cases(config.plan, tuple(cases))
    report = read_eval_suite_report(report_path)
    if report != expected:
        raise KernelError("eval_suite_report_mismatch", "Suite报告与Case证据不一致")
    digest = eval_suite_report_sha256(report)
    if state.status == "completed":
        if state.report_sha256 != digest:
            raise KernelError("eval_suite_report_mismatch", "Suite报告摘要不一致")
        return _run_report(config, state, "completed", published=True)
    state = state.model_copy(
        update={
            "status": "completed",
            "current_case_id": None,
            "stop_reason": None,
            "report_sha256": digest,
            "updated_at": utc_now(),
        }
    )
    write_eval_suite_execution_state(root / _STATE_FILE, state)
    return _run_report(config, state, "completed", published=True)


def _prepare_case_roots(root: Path, count: int) -> None:
    ensure_private_directory(
        root / _CASES_DIRECTORY,
        error_code="eval_suite_work_root_invalid",
        label="Suite Case目录",
    )
    for index in range(count):
        ensure_private_directory(
            _case_root(root, index),
            error_code="eval_suite_work_root_invalid",
            label="Suite Case目录",
        )


def _load_or_create_state(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    execution_binding_sha256: str | None,
) -> CodingEvalSuiteExecutionState:
    state_path = root / _STATE_FILE
    if path_present(state_path):
        return read_eval_suite_execution_state(state_path)
    state = _initial_state(config, execution_binding_sha256)
    write_eval_suite_execution_state(state_path, state)
    return state


def _boundary_stop(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    state: CodingEvalSuiteExecutionState,
    *,
    known_units: int,
    all_costs_complete: bool,
    completed_count: int,
) -> CodingEvalSuiteRunReport | None:
    if not all_costs_complete:
        return _stop(root, config, state, "cost_unknown", current_case_id=None)
    if known_units >= amount_units(config.fee_stop_amount) and completed_count < len(
        config.plan.cases
    ):
        return _stop(root, config, state, "fee_limit_reached", current_case_id=None)
    return None


def _running_state(
    root: Path,
    state: CodingEvalSuiteExecutionState,
    case_id: str,
) -> CodingEvalSuiteExecutionState:
    state = state.model_copy(
        update={
            "status": "running",
            "current_case_id": case_id,
            "stop_reason": None,
            "updated_at": utc_now(),
        }
    )
    write_eval_suite_execution_state(root / _STATE_FILE, state)
    return state


async def _invoke_case(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    case_executor: SuiteCaseExecutor,
    token: CancelToken,
    index: int,
) -> CodingEvalSuiteCaseRunResult:
    expected = config.plan.cases[index]
    try:
        token.checkpoint()
        result = await case_executor(
            expected,
            config.campaign_plans[index],
            _case_root(root, index),
            token,
        )
    except TurnCancelled:
        return CodingEvalSuiteCaseRunResult(case_id=expected.case_id, reason="cancelled")
    try:
        result = CodingEvalSuiteCaseRunResult.model_validate_json(
            result.model_dump_json(), strict=True
        )
    except (AttributeError, ValueError):
        raise KernelError("eval_suite_case_result_invalid", "Suite Case执行结果无效") from None
    if result.case_id != expected.case_id:
        raise KernelError("eval_suite_case_mismatch", "Suite Case执行结果身份不匹配")
    return result


def _commit_case_report(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    state: CodingEvalSuiteExecutionState,
    completed: list[CodingEvalSuiteCaseReport],
    result: CodingEvalSuiteCaseRunResult,
    index: int,
    known_units: int,
    fail: Fault,
) -> tuple[CodingEvalSuiteExecutionState, int, bool]:
    assert result.report is not None
    _require_case_report(result.report, config.plan.cases[index], config, index)
    write_eval_suite_case_report(
        _case_root(root, index) / _CASE_REPORT_FILE,
        result.report,
    )
    fail("suite.after_case_evidence")
    completed.append(result.report)
    case_units, case_cost_complete = _case_cost(result.report, config.fee_stop_currency)
    known_units += case_units
    state = state.model_copy(
        update={
            "completed_case_ids": tuple(
                case.case_id for case in config.plan.cases[: len(completed)]
            ),
            "current_case_id": None,
            "known_cost_amount": format_amount(known_units),
            "updated_at": utc_now(),
        }
    )
    write_eval_suite_execution_state(root / _STATE_FILE, state)
    fail("suite.after_case_state")
    return state, known_units, case_cost_complete


def _publish_suite_report(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    state: CodingEvalSuiteExecutionState,
    completed: list[CodingEvalSuiteCaseReport],
    fail: Fault,
) -> CodingEvalSuiteRunReport:
    report = build_coding_eval_suite_report_from_cases(config.plan, tuple(completed))
    write_eval_suite_report(root / _REPORT_FILE, report)
    fail("suite.after_report")
    state = state.model_copy(
        update={
            "status": "completed",
            "current_case_id": None,
            "stop_reason": None,
            "report_sha256": eval_suite_report_sha256(report),
            "updated_at": utc_now(),
        }
    )
    write_eval_suite_execution_state(root / _STATE_FILE, state)
    return _run_report(config, state, "completed", published=True)


async def _execute_remaining_cases(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    case_executor: SuiteCaseExecutor,
    token: CancelToken,
    fail: Fault,
    state: CodingEvalSuiteExecutionState,
    completed: list[CodingEvalSuiteCaseReport],
    known_units: int,
) -> CodingEvalSuiteRunReport:
    for index in range(len(completed), len(config.plan.cases)):
        expected = config.plan.cases[index]
        state = _running_state(root, state, expected.case_id)
        result = await _invoke_case(root, config, case_executor, token, index)
        if result.reason != "completed":
            return _stop(
                root,
                config,
                state,
                result.reason,
                current_case_id=expected.case_id,
            )
        state, known_units, cost_complete = _commit_case_report(
            root,
            config,
            state,
            completed,
            result,
            index,
            known_units,
            fail,
        )
        stopped = _boundary_stop(
            root,
            config,
            state,
            known_units=known_units,
            all_costs_complete=cost_complete,
            completed_count=len(completed),
        )
        if stopped is not None:
            return stopped
    return _publish_suite_report(root, config, state, completed, fail)


async def _run_locked_suite(
    root: Path,
    config: CodingEvalSuiteRunConfig,
    case_executor: SuiteCaseExecutor,
    token: CancelToken,
    resume: bool,
    fail: Fault,
    execution_binding_sha256: str | None,
) -> CodingEvalSuiteRunReport:
    _require_plan(root / _PLAN_FILE, config)
    fail("suite.after_plan")
    _prepare_case_roots(root, len(config.plan.cases))
    state = _load_or_create_state(root, config, execution_binding_sha256)
    fail("suite.after_state")
    _require_state(state, config, execution_binding_sha256)
    state, completed, known_units, all_costs_complete = _reconcile_prefix(root, config, state)
    recovered = _recover_published_report(root, config, state, completed)
    if recovered is not None:
        return recovered
    if state.status == "completed":
        raise KernelError("eval_suite_report_mismatch", "Suite完成状态缺少报告")
    if state.status == "stopped" and not resume:
        assert state.stop_reason is not None
        return _run_report(config, state, state.stop_reason)
    stopped = _boundary_stop(
        root,
        config,
        state,
        known_units=known_units,
        all_costs_complete=all_costs_complete,
        completed_count=len(completed),
    )
    if stopped is not None:
        return stopped
    return await _execute_remaining_cases(
        root,
        config,
        case_executor,
        token,
        fail,
        state,
        completed,
        known_units,
    )


async def run_coding_eval_suite(
    config: CodingEvalSuiteRunConfig,
    case_executor: SuiteCaseExecutor,
    *,
    cancel: CancelToken | None = None,
    resume: bool = False,
    fault: Fault | None = None,
    execution_binding_sha256: str | None = None,
) -> CodingEvalSuiteRunReport:
    """顺序执行固定Case；崩溃只重入当前Case，停止状态必须显式恢复。"""

    try:
        config = CodingEvalSuiteRunConfig.model_validate_json(config.model_dump_json(), strict=True)
    except ValueError:
        raise KernelError("eval_suite_config_invalid", "Suite执行配置无效") from None
    if execution_binding_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", execution_binding_sha256
    ):
        raise KernelError("eval_suite_config_invalid", "Suite执行绑定摘要无效")
    root = Path(config.work_root)
    ensure_private_directory(
        root,
        error_code="eval_suite_work_root_invalid",
        label="Suite私有运行目录",
    )
    with exclusive_execution_lock(
        root,
        _LOCK_FILE,
        busy_code="eval_suite_busy",
        invalid_code="eval_suite_lock_invalid",
        label="Suite",
    ):
        return await _run_locked_suite(
            root,
            config,
            case_executor,
            cancel or CancelToken(),
            resume,
            fault or _fault,
            execution_binding_sha256,
        )
