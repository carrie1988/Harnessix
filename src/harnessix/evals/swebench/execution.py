"""顺序执行预注册 SWE-bench Agent Case，并冻结完整 predictions。"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    Turn,
    TurnStatusV18,
)
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
from harnessix.evals.swebench.disk_space import require_evaluation_disk_space
from harnessix.evals.swebench.execution_contracts import (
    SweBenchCaseExecutionState,
    SweBenchSuiteExecutionState,
    build_swebench_case_execution_state,
    build_swebench_suite_execution_state,
    is_swebench_isolated_provider_failure,
    is_swebench_quality_failure,
)
from harnessix.evals.swebench.execution_costs import (
    _can_isolate_transport_failure,
    _next_case_reserve,
    _turn_cost,
    _unknown_request_reserve,
)
from harnessix.evals.swebench.execution_evidence import (
    _case_evidence_failure,
    _require_case_evidence,
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
from harnessix.evals.swebench.run_config import SweBenchRunConfig as SweBenchAgentRunConfig
from harnessix.evals.swebench.source_revision import require_harnessix_execution_revision
from harnessix.evals.swebench.workspace import materialize_swebench_workspace
from harnessix.evals.swebench.workspace_retirement import (
    read_retired_patch,
    seal_and_retire_workspace,
)
from harnessix.models.pricing import amount_units, format_amount
from harnessix.product_config.action_contracts import ProductProcessProfile
from harnessix.product_config.process_profile import process_tool_name
from harnessix.tools.runtime import _drain

_RUN_NAMESPACE = UUID("be7c86aa-71bb-5fdc-9bb4-47612c041d40")
_CASES_DIRECTORY = "cases"
_RUN_DIRECTORY = "run"
_CASE_STATE_FILE = "case-state.json"
_SUITE_STATE_FILE = "suite-state.json"
_PREDICTIONS_FILE = "predictions.jsonl"
_LOCK_FILE = ".agent-suite.lock"
_SESSION_FILE = "session.sqlite"


def _case_root(root: Path, index: int) -> Path:
    return root / _CASES_DIRECTORY / f"case-{index:03d}"


def _run_id(plan: SweBenchPreRegistrationPlan, case: SweBenchPublicCase) -> UUID:
    return uuid5(_RUN_NAMESPACE, f"{plan.plan_sha256}:{case.instance_id}")


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
    *,
    isolate_provider_failures: bool = False,
) -> SweBenchSuiteExecutionState:
    path = root / _SUITE_STATE_FILE
    previous = read_suite_execution_state(path, plan) if path_present(path) else None
    if previous is not None and (
        (previous.spec_version == "harnessix.swebench-suite-execution-state/v3")
        != isolate_provider_failures
    ):
        raise KernelError(
            "swebench_suite_execution_state_invalid", "SWE-bench冻结的故障隔离规则发生漂移"
        )
    rebuilt = build_swebench_suite_execution_state(
        plan,
        states,
        continue_quality_failures=(
            previous is None
            or previous.spec_version != "harnessix.swebench-suite-execution-state/v1"
        ),
        continue_provider_failures=isolate_provider_failures,
    )
    if previous is not None:
        previous_count = len(previous.attempted_case_ids)
        if previous == rebuilt:
            return previous
        if (
            previous.attempted_case_ids != rebuilt.attempted_case_ids[:previous_count]
            or previous.case_states[:previous_count] != rebuilt.case_states[:previous_count]
            or len(rebuilt.attempted_case_ids) not in {previous_count, previous_count + 1}
        ):
            raise KernelError(
                "swebench_suite_execution_state_invalid",
                "SWE-bench Suite状态不能从Case证据安全恢复",
            )
    write_suite_execution_state(path, plan, rebuilt)
    return rebuilt


async def _materialize_case(
    config: SweBenchAgentRunConfig,
    case: SweBenchPublicCase,
    state: SweBenchCaseExecutionState,
    index: int,
    *,
    allow_network: bool,
) -> Path:
    """只为即将执行/核验的一项准备工作区，失败不预建剩余24项。"""
    workspaces_root = Path(config.work_root) / "workspaces"
    cache_root = Path(config.cache_root)
    run_root = _case_root(Path(config.work_root), index) / _RUN_DIRECTORY
    may_contain_agent_changes = state.status == "completed" or path_present(
        run_root / _SESSION_FILE
    )
    require_evaluation_disk_space(workspaces_root, cache_root)
    return await asyncio.to_thread(
        materialize_swebench_workspace,
        case,
        workspaces_root=workspaces_root,
        cache_root=cache_root,
        git_executable=Path(config.git_executable),
        allow_network=allow_network,
        require_clean=not may_contain_agent_changes,
    )


async def _open_case_turn(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchAgentRunConfig,
    case: SweBenchPublicCase,
    case_root: Path,
    workspace: Path,
    process_profile: ProductProcessProfile,
    api_key: str,
    cancel: CancelToken,
) -> tuple[UUID, Turn]:
    run_root = case_root / _RUN_DIRECTORY
    state = read_case_execution_state(case_root / _CASE_STATE_FILE, plan)
    expected_terminal_turn = None
    if state.status in {"completed", "failed", "cancelled"}:
        if state.thread_id is None or state.turn_id is None:
            raise KernelError("swebench_case_evidence_mismatch", "SWE-bench终态缺少原生身份")
        expected_terminal_turn = (state.thread_id, state.turn_id)
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
        process_profile=process_profile,
        api_key=api_key,
        cancel=cancel,
        expected_terminal_turn=expected_terminal_turn,
        budget_pressure_compaction=config.budget_pressure_compaction,
        enable_planning=config.planning_enabled,
        enable_grep_snapshots=config.grep_snapshots_enabled,
        allow_optional_pre_edit_profile=config.optional_pre_edit_profile_enabled,
        enable_regex_search=config.regex_search_enabled,
        reserve_closing_phase=config.closing_phase_enabled,
    )


async def _retire_case_workspace(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchAgentRunConfig,
    index: int,
    state: SweBenchCaseExecutionState,
    patch: str,
    api_key: str,
    cancel: CancelToken,
) -> None:
    """持有Suite锁直到清理线程结算；父Task取消也传播至每个删除检查点。"""
    cancel.checkpoint()
    worker = asyncio.create_task(
        asyncio.to_thread(
            seal_and_retire_workspace,
            plan,
            config,
            index,
            state,
            patch,
            api_key=api_key,
            cancel=cancel,
        )
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.cancel()
        await _drain(worker)
        raise


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
    require_evaluation_disk_space(root, Path(config.cache_root), Path(config.source_root))
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
        suite = _reconcile_suite_state(
            root, plan, states, isolate_provider_failures=config.isolate_provider_failures
        )
        continue_quality_failures = (
            suite.spec_version != "harnessix.swebench-suite-execution-state/v1"
        )
        known_cost_units = 0
        reserved_cost_units = 0
        unaccounted_cases = 0
        completed_count = 0
        attempted_patches: list[str] = []
        for index, case in enumerate(plan.cases):
            token.checkpoint()
            state = states[index]
            terminal_quality_failure = continue_quality_failures and is_swebench_quality_failure(
                state
            )
            terminal_provider_failure = (
                config.isolate_provider_failures and is_swebench_isolated_provider_failure(state)
            )
            terminal_attempt = (
                state.status == "completed" or terminal_quality_failure or terminal_provider_failure
            )
            if state.status in {"failed", "cancelled"} and not terminal_attempt:
                raise KernelError("swebench_execution_stopped", "SWE-bench Suite此前已停止")
            if not terminal_attempt and not (
                config.price.valid_from <= utc_now() < config.price.valid_until
            ):
                raise KernelError("swebench_price_expired", "SWE-bench固定价格快照已过期")
            if not terminal_attempt and (
                known_cost_units + reserved_cost_units + _next_case_reserve(plan, config)
                > amount_units(config.fee_stop_amount)
            ):
                raise KernelError("swebench_fee_limit_reached", "SWE-bench费用达到停止线")
            process_profile = config.process_profile_for(case.instance_id)
            if not terminal_attempt:
                require_evaluation_disk_space(root, Path(config.cache_root))
            retired_patch = (
                read_retired_patch(plan, config, index, api_key=api_key)
                if config.workspace_retirement_enabled and terminal_attempt
                else None
            )
            if retired_patch is not None:
                workspace = Path(config.work_root) / "workspaces" / case.instance_id
            else:
                workspace = await _materialize_case(
                    config, case, state, index, allow_network=allow_repository_network
                )
            thread_id, turn = await _open_case_turn(
                plan,
                config,
                case,
                _case_root(root, index),
                workspace,
                process_profile,
                api_key,
                token,
            )
            if turn.status not in TERMINAL_TURNS:
                raise KernelError("swebench_turn_incomplete", "SWE-bench Turn未进入终态")
            patch = (
                retired_patch
                if retired_patch is not None
                else collect_workspace_patch(
                    workspace, Path(config.git_executable), case.base_commit
                )
            )
            case_cost, cost_complete = _turn_cost(turn, config)
            known_cost_units += case_cost
            if not cost_complete:
                reserved_cost_units += _unknown_request_reserve(config)
                unaccounted_cases += 1
            if known_cost_units + reserved_cost_units > amount_units(config.fee_stop_amount):
                raise KernelError("swebench_fee_limit_reached", "SWE-bench费用达到停止线")

            evidence_failure = _case_evidence_failure(
                turn,
                patch,
                process_tool_name(process_profile.profile_id),
                cost_complete=cost_complete,
                transport_failure_isolatable=(
                    config.isolate_provider_failures
                    and _can_isolate_transport_failure(turn, config)
                ),
            )
            if terminal_attempt:
                _require_case_evidence(state, thread_id, turn, patch)
                expected_failure = evidence_failure[0] if evidence_failure else None
                if state.error_code != expected_failure:
                    raise KernelError(
                        "swebench_case_evidence_mismatch", "SWE-bench失败边界发生漂移"
                    )
            else:
                failure_code = evidence_failure[0] if evidence_failure is not None else None
                state = build_swebench_case_execution_state(
                    plan,
                    case.instance_id,
                    state.run_id,
                    status="failed" if evidence_failure is not None else "completed",
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    turn_status=TurnStatusV18(turn.status),
                    patch_sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                    input_tokens=turn.usage.input_tokens,
                    output_tokens=turn.usage.output_tokens,
                    error_code=failure_code,
                    started_at=state.started_at,
                    updated_at=turn.completed_at or state.updated_at,
                )
                states[index] = state
                write_case_execution_state(
                    _case_root(root, index) / _CASE_STATE_FILE,
                    plan,
                    state,
                )
                _reconcile_suite_state(
                    root, plan, states, isolate_provider_failures=config.isolate_provider_failures
                )
            if evidence_failure is not None and not (
                continue_quality_failures
                and is_swebench_quality_failure(state)
                or config.isolate_provider_failures
                and is_swebench_isolated_provider_failure(state)
            ):
                code, message = evidence_failure
                if not cost_complete:
                    message += (
                        f"；未决费用保留全额预留 {config.price.currency} "
                        f"{format_amount(reserved_cost_units)}"
                    )
                raise KernelError(code, message)
            attempted_patches.append(patch)
            completed_count += state.status == "completed"
            if config.workspace_retirement_enabled:
                _require_case_evidence(state, thread_id, turn, patch)
                await _retire_case_workspace(plan, config, index, state, patch, api_key, token)

        suite = _reconcile_suite_state(
            root, plan, states, isolate_provider_failures=config.isolate_provider_failures
        )
        if suite.status != "completed":
            raise KernelError("swebench_execution_incomplete", "SWE-bench Suite证据未完整闭环")
        predictions = tuple(
            SweBenchPrediction(
                instance_id=case.instance_id,
                model_name_or_path=plan.model_name,
                model_patch=patch,
            )
            for case, patch in zip(plan.cases, attempted_patches, strict=True)
        )
        predictions_sha256 = write_predictions(
            root / _PREDICTIONS_FILE,
            plan,
            predictions,
        )
        return {
            "reason": "completed",
            "plan_sha256": plan.plan_sha256,
            "attempted_cases": len(attempted_patches),
            "completed_cases": completed_count,
            "quality_failed_cases": sum(is_swebench_quality_failure(state) for state in states),
            "provider_failed_cases": sum(
                is_swebench_isolated_provider_failure(state) for state in states
            ),
            "input_tokens": suite.input_tokens,
            "output_tokens": suite.output_tokens,
            "known_cost_currency": config.price.currency,
            "known_cost_amount": format_amount(known_cost_units),
            "reserved_cost_amount": format_amount(reserved_cost_units),
            "unaccounted_cost_cases": unaccounted_cases,
            "predictions_sha256": predictions_sha256,
        }
