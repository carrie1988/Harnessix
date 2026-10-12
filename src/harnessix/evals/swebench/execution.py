"""顺序执行预注册 SWE-bench Agent Case，并冻结完整 predictions。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TERMINAL_TURNS, Turn, TurnStatusV18
from harnessix.domain.models import utc_now
from harnessix.evals.execution_fs import (
    ensure_private_directory,
    exclusive_execution_lock,
    path_present,
)
from harnessix.evals.swebench.agent_execution import run_swebench_agent_case
from harnessix.evals.swebench.contracts import (
    SweBenchPreRegistrationPlan,
    SweBenchPublicCase,
)
from harnessix.evals.swebench.execution_contracts import (
    SweBenchCaseExecutionState,
    SweBenchSuiteExecutionState,
    build_swebench_case_execution_state,
    build_swebench_suite_execution_state,
)
from harnessix.evals.swebench.execution_io import (
    read_case_execution_state,
    read_suite_execution_state,
    write_case_execution_state,
    write_suite_execution_state,
)
from harnessix.evals.swebench.predictions import (
    SweBenchPrediction,
    collect_workspace_patch,
    write_predictions,
)
from harnessix.evals.swebench.run_config import SweBenchAgentRunConfig
from harnessix.evals.swebench.source_revision import require_harnessix_execution_revision
from harnessix.evals.swebench.workspace import materialize_swebench_workspace
from harnessix.models.costs import bind_price, build_cost_report
from harnessix.models.pricing import amount_units, format_amount

_RUN_NAMESPACE = UUID("be7c86aa-71bb-5fdc-9bb4-47612c041d40")
_CASES_DIRECTORY = "cases"
_RUN_DIRECTORY = "run"
_CASE_STATE_FILE = "case-state.json"
_SUITE_STATE_FILE = "suite-state.json"
_PREDICTIONS_FILE = "predictions.jsonl"
_LOCK_FILE = ".agent-suite.lock"
_SESSION_FILE = "session.sqlite"
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def _case_root(root: Path, index: int) -> Path:
    return root / _CASES_DIRECTORY / f"case-{index:03d}"


def _run_id(plan: SweBenchPreRegistrationPlan, case: SweBenchPublicCase) -> UUID:
    return uuid5(_RUN_NAMESPACE, f"{plan.plan_sha256}:{case.instance_id}")


def _stable_error_code(turn: Turn) -> str | None:
    if turn.status.value == "completed":
        return None
    code = turn.error.code if turn.error is not None else f"turn_{turn.status.value}"
    return code if _ERROR_CODE.fullmatch(code) else "turn_failed"


def _turn_cost(turn: Turn, config: SweBenchAgentRunConfig) -> tuple[int, bool]:
    if not turn.accounted_attempts:
        return 0, False
    bindings = tuple(
        bind_price(attempt, config.price, config.billing_context)
        for attempt in turn.accounted_attempts
    )
    report = build_cost_report(turn, bindings)
    totals = report.summary.totals
    if len(totals) > 1 or (totals and totals[0].currency != config.price.currency):
        raise KernelError("swebench_cost_invalid", "SWE-bench模型费用币种无效")
    units = amount_units(totals[0].known_amount) if totals else 0
    return units, report.summary.completeness == "complete"


def _require_case_evidence(
    state: SweBenchCaseExecutionState,
    thread_id: UUID,
    turn: Turn,
    patch: str,
) -> None:
    if (
        state.thread_id != thread_id
        or state.turn_id != turn.turn_id
        or state.turn_status != TurnStatusV18(turn.status)
        or state.input_tokens != turn.usage.input_tokens
        or state.output_tokens != turn.usage.output_tokens
        or state.patch_sha256 != hashlib.sha256(patch.encode("utf-8")).hexdigest()
    ):
        raise KernelError("swebench_case_evidence_mismatch", "SWE-bench Case证据与状态不一致")


def _load_or_initialize_case_states(
    root: Path,
    plan: SweBenchPreRegistrationPlan,
    started_at: datetime,
) -> list[SweBenchCaseExecutionState]:
    cases_root = root / _CASES_DIRECTORY
    ensure_private_directory(
        cases_root,
        error_code="swebench_execution_root_invalid",
        label="SWE-bench Case目录",
    )
    states: list[SweBenchCaseExecutionState] = []
    for index, case in enumerate(plan.cases):
        case_root = _case_root(root, index)
        ensure_private_directory(
            case_root,
            error_code="swebench_execution_root_invalid",
            label="SWE-bench Case运行目录",
        )
        state_path = case_root / _CASE_STATE_FILE
        if path_present(state_path):
            state = read_case_execution_state(state_path, plan)
        else:
            state = build_swebench_case_execution_state(
                plan,
                case.instance_id,
                _run_id(plan, case),
                started_at=started_at,
                updated_at=started_at,
            )
            write_case_execution_state(state_path, plan, state)
        states.append(state)
    return states


def _reconcile_suite_state(
    root: Path,
    plan: SweBenchPreRegistrationPlan,
    states: list[SweBenchCaseExecutionState],
) -> SweBenchSuiteExecutionState:
    rebuilt = build_swebench_suite_execution_state(plan, states)
    path = root / _SUITE_STATE_FILE
    if path_present(path):
        previous = read_suite_execution_state(path, plan)
        previous_count = len(previous.completed_case_ids)
        if previous == rebuilt:
            return previous
        if (
            previous.completed_case_ids != rebuilt.completed_case_ids[:previous_count]
            or previous.case_states[:previous_count] != rebuilt.case_states[:previous_count]
            or len(rebuilt.completed_case_ids) not in {previous_count, previous_count + 1}
        ):
            raise KernelError(
                "swebench_suite_execution_state_invalid",
                "SWE-bench Suite状态不能从Case证据安全恢复",
            )
    write_suite_execution_state(path, plan, rebuilt)
    return rebuilt


async def _materialize_all(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchAgentRunConfig,
    states: list[SweBenchCaseExecutionState],
    *,
    allow_network: bool,
) -> tuple[Path, ...]:
    workspaces_root = Path(config.work_root) / "workspaces"
    cache_root = Path(config.cache_root)
    semaphore = asyncio.Semaphore(4)
    repository_locks = {case.repo: asyncio.Lock() for case in plan.cases}

    async def materialize(index: int, case: SweBenchPublicCase) -> Path:
        run_root = _case_root(Path(config.work_root), index) / _RUN_DIRECTORY
        may_contain_agent_changes = states[index].status == "completed" or path_present(
            run_root / _SESSION_FILE
        )
        async with semaphore, repository_locks[case.repo]:
            return await asyncio.to_thread(
                materialize_swebench_workspace,
                case,
                workspaces_root=workspaces_root,
                cache_root=cache_root,
                git_executable=Path(config.git_executable),
                allow_network=allow_network,
                require_clean=not may_contain_agent_changes,
            )

    return tuple(
        await asyncio.gather(*(materialize(index, case) for index, case in enumerate(plan.cases)))
    )


async def _open_case_turn(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchAgentRunConfig,
    case: SweBenchPublicCase,
    case_root: Path,
    workspace: Path,
    api_key: str,
    cancel: CancelToken,
) -> tuple[UUID, Turn]:
    run_root = case_root / _RUN_DIRECTORY
    ensure_private_directory(
        run_root,
        error_code="swebench_execution_root_invalid",
        label="SWE-bench Session运行目录",
    )
    return await run_swebench_agent_case(
        plan=plan,
        case=case,
        run_root=run_root,
        workspace=workspace,
        git_executable=Path(config.git_executable),
        provider_config=config.provider_config,
        execution_fingerprint=config.fingerprint,
        api_key=api_key,
        cancel=cancel,
    )


async def run_swebench_agent_suite(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchAgentRunConfig,
    *,
    api_key: str,
    allow_repository_network: bool,
    allow_provider_network: bool,
    cancel: CancelToken | None = None,
) -> dict[str, object]:
    """执行或恢复同一 pass@1 Suite；任何 Case 都不创建第二个 Turn。"""

    if not allow_provider_network:
        raise KernelError("swebench_network_not_enabled", "SWE-bench Provider联网未启用")
    if not api_key:
        raise KernelError("swebench_provider_key_missing", "SWE-bench Provider凭据缺失")
    try:
        config.require_plan(plan)
    except ValueError:
        raise KernelError("swebench_run_config_invalid", "SWE-bench执行配置与计划不一致") from None
    root = Path(config.work_root)
    ensure_private_directory(
        root,
        error_code="swebench_execution_root_invalid",
        label="SWE-bench私有运行目录",
    )
    token = cancel or CancelToken()
    with exclusive_execution_lock(
        root,
        _LOCK_FILE,
        busy_code="swebench_execution_busy",
        invalid_code="swebench_execution_root_invalid",
        label="SWE-bench Agent Suite",
    ):
        require_harnessix_execution_revision(
            Path(config.source_root),
            Path(config.git_executable),
            plan.harnessix_revision,
        )
        states = _load_or_initialize_case_states(root, plan, config.price.valid_from)
        _reconcile_suite_state(root, plan, states)
        workspaces = await _materialize_all(
            plan,
            config,
            states,
            allow_network=allow_repository_network,
        )

        known_cost_units = 0
        completed_count = 0
        for index, (case, workspace) in enumerate(zip(plan.cases, workspaces, strict=True)):
            token.checkpoint()
            state = states[index]
            if state.status == "failed":
                raise KernelError("swebench_execution_stopped", "SWE-bench Suite此前已停止")
            if state.status != "completed" and not (
                config.price.valid_from <= utc_now() < config.price.valid_until
            ):
                raise KernelError("swebench_price_expired", "SWE-bench固定价格快照已过期")
            thread_id, turn = await _open_case_turn(
                plan,
                config,
                case,
                _case_root(root, index),
                workspace,
                api_key,
                token,
            )
            if turn.status not in TERMINAL_TURNS:
                raise KernelError("swebench_turn_incomplete", "SWE-bench Turn未进入终态")
            patch = collect_workspace_patch(
                workspace,
                Path(config.git_executable),
                case.base_commit,
            )
            case_cost, cost_complete = _turn_cost(turn, config)
            if not cost_complete:
                stopped = build_swebench_case_execution_state(
                    plan,
                    case.instance_id,
                    state.run_id,
                    status="failed",
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    turn_status=TurnStatusV18(turn.status),
                    patch_sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                    input_tokens=turn.usage.input_tokens,
                    output_tokens=turn.usage.output_tokens,
                    error_code="cost_unknown",
                    started_at=state.started_at,
                    updated_at=turn.completed_at or state.updated_at,
                )
                states[index] = stopped
                write_case_execution_state(
                    _case_root(root, index) / _CASE_STATE_FILE,
                    plan,
                    stopped,
                )
                _reconcile_suite_state(root, plan, states)
                raise KernelError("swebench_cost_unknown", "SWE-bench模型费用无法完整结算")
            known_cost_units += case_cost
            if known_cost_units > amount_units(config.fee_stop_amount):
                raise KernelError("swebench_fee_limit_reached", "SWE-bench费用达到停止线")

            if state.status == "completed":
                _require_case_evidence(state, thread_id, turn, patch)
            else:
                state = build_swebench_case_execution_state(
                    plan,
                    case.instance_id,
                    state.run_id,
                    status="completed",
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    turn_status=TurnStatusV18(turn.status),
                    patch_sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                    input_tokens=turn.usage.input_tokens,
                    output_tokens=turn.usage.output_tokens,
                    error_code=_stable_error_code(turn),
                    started_at=state.started_at,
                    updated_at=turn.completed_at or state.updated_at,
                )
                states[index] = state
                write_case_execution_state(
                    _case_root(root, index) / _CASE_STATE_FILE,
                    plan,
                    state,
                )
                _reconcile_suite_state(root, plan, states)
            completed_count += 1

        predictions = tuple(
            SweBenchPrediction(
                instance_id=case.instance_id,
                model_name_or_path=plan.model_name,
                model_patch=collect_workspace_patch(
                    workspace,
                    Path(config.git_executable),
                    case.base_commit,
                ),
            )
            for case, workspace in zip(plan.cases, workspaces, strict=True)
        )
        predictions_sha256 = write_predictions(
            root / _PREDICTIONS_FILE,
            plan,
            predictions,
        )
        suite = _reconcile_suite_state(root, plan, states)
        return {
            "reason": "completed",
            "plan_sha256": plan.plan_sha256,
            "completed_cases": completed_count,
            "input_tokens": suite.input_tokens,
            "output_tokens": suite.output_tokens,
            "known_cost_currency": config.price.currency,
            "known_cost_amount": format_amount(known_cost_units),
            "predictions_sha256": predictions_sha256,
        }
