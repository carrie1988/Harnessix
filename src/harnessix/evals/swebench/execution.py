"""顺序执行预注册 SWE-bench Agent Case，并冻结完整 predictions。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    ItemStatus,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    Turn,
    TurnStatus,
    TurnStatusV18,
)
from harnessix.agent.tool_rejections import require_closed_rejection_items
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
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
    is_swebench_quality_failure,
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
from harnessix.models.pricing import FlatInputPrice, amount_units, format_amount, rate_units
from harnessix.product_config.action_contracts import ProductProcessProfile
from harnessix.product_config.process_profile import process_tool_name
from harnessix.tools.git_contracts import GitReviewOutput

_RUN_NAMESPACE = UUID("be7c86aa-71bb-5fdc-9bb4-47612c041d40")
_CASES_DIRECTORY = "cases"
_RUN_DIRECTORY = "run"
_CASE_STATE_FILE = "case-state.json"
_SUITE_STATE_FILE = "suite-state.json"
_PREDICTIONS_FILE = "predictions.jsonl"
_LOCK_FILE = ".agent-suite.lock"
_SESSION_FILE = "session.sqlite"
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_PATCH_TOOLS = frozenset({"apply_patch", WORKSPACE_PATCH_TOOL})
_GIT_REVIEW_TOOLS = frozenset({"git_status", "git_diff"})


@dataclass(frozen=True, slots=True)
class _ToolObservation:
    index: int
    call: ToolCallContent
    result: ToolResultContent

    @property
    def tool(self) -> str:
        return self.call.tool


def _case_root(root: Path, index: int) -> Path:
    return root / _CASES_DIRECTORY / f"case-{index:03d}"


def _run_id(plan: SweBenchPreRegistrationPlan, case: SweBenchPublicCase) -> UUID:
    return uuid5(_RUN_NAMESPACE, f"{plan.plan_sha256}:{case.instance_id}")


def _stable_error_code(turn: Turn) -> str | None:
    if turn.status.value == "completed":
        return None
    code = turn.error.code if turn.error is not None else f"turn_{turn.status.value}"
    return code if _ERROR_CODE.fullmatch(code) else "turn_failed"


def _tool_observations(turn: Turn) -> tuple[_ToolObservation, ...]:
    require_closed_rejection_items(turn.items)
    calls: dict[UUID, ToolCallContent | ToolCallRejectionContent] = {}
    settled: set[UUID] = set()
    observations: list[_ToolObservation] = []
    for index, item in enumerate(turn.items):
        content = item.content
        if not isinstance(content, (ToolCallContent, ToolCallRejectionContent, ToolResultContent)):
            continue
        if item.status is not ItemStatus.COMPLETED or item.error is not None:
            raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具事实未完整结算")
        if isinstance(content, (ToolCallContent, ToolCallRejectionContent)):
            if content.call_id in calls:
                raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具调用身份重复")
            calls[content.call_id] = content
        else:
            call = calls.get(content.call_id)
            if call is None or content.call_id in settled:
                raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具结果配对无效")
            settled.add(content.call_id)
            if isinstance(call, ToolCallContent):
                observations.append(_ToolObservation(index, call, content))
    if calls.keys() != settled:
        raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具调用缺少结算结果")
    return tuple(observations)


def _is_code_test_failure(observation: _ToolObservation) -> bool:
    return (
        observation.result.outcome == "failed"
        and observation.result.error is not None
        and observation.result.error.code == "process_nonzero_exit"
    )


def _case_evidence_failure(
    turn: Turn,
    patch: str,
    public_test_tool: str,
    *,
    cost_complete: bool,
) -> tuple[str, str] | None:
    try:
        observations = _tool_observations(turn)
    except KernelError:
        return "swebench_tool_state_unavailable", "SWE-bench工具事实未完整闭合"
    if any(
        item.tool != public_test_tool and item.result.outcome in {"unknown", "cancelled"}
        for item in observations
    ):
        return "swebench_tool_state_unavailable", "SWE-bench工具状态未知或已取消"
    public_tests = tuple(item for item in observations if item.tool == public_test_tool)
    if any(
        item.result.outcome != "succeeded" and not _is_code_test_failure(item)
        for item in public_tests
    ):
        return (
            "swebench_public_tests_unavailable",
            "SWE-bench公开测试发生非代码执行失败",
        )

    if not cost_complete:
        return "swebench_cost_incomplete", "SWE-bench模型费用未完整结算"
    if turn.status is not TurnStatus.COMPLETED:
        code = _stable_error_code(turn) or "swebench_turn_failed"
        # 仅映射已计费、响应身份已知的契约失败；原 Turn 和请求账本保持不变。
        if (
            turn.status is TurnStatus.FAILED
            and code == "invalid_provider_output"
            and turn.accounted_attempts
            and all(
                attempt.response_id is not None and attempt.status in {"completed", "failed"}
                for attempt in turn.accounted_attempts
            )
        ):
            code = "swebench_model_response_invalid"
        return code, "SWE-bench Case Turn未成功完成"

    patch_results = tuple(item for item in observations if item.tool in _PATCH_TOOLS)
    if not patch_results:
        return "swebench_patch_missing", "SWE-bench Case未产生成功 Patch"
    final_patch = patch_results[-1]
    if final_patch.result.outcome != "succeeded":
        return "swebench_patch_failed", "SWE-bench Case最终 Patch未成功"
    if not patch.strip():
        return "swebench_patch_missing", "SWE-bench Case最终 Git Patch为空"

    final_public_tests = tuple(item for item in public_tests if item.index > final_patch.index)
    if not final_public_tests:
        return "swebench_public_tests_missing", "SWE-bench最终 Patch后缺少公开测试"
    if any(item.result.outcome != "succeeded" for item in final_public_tests):
        return "swebench_public_tests_failed", "SWE-bench最终 Patch后的公开测试未通过"
    final_test_index = final_public_tests[-1].index

    reviews = tuple(
        item for item in observations if item.index > final_test_index and item.tool == "git_review"
    )
    if reviews:
        review = reviews[-1].result
        if review.outcome == "succeeded":
            try:
                output = GitReviewOutput.model_validate_json(json.dumps(review.output))
            except (ValueError, TypeError):
                pass
            else:
                if not output.status.truncated and not output.diff.truncated:
                    return None
        return "swebench_git_review_incomplete", "SWE-bench Git复核输出失败、无效或被截断"

    final_reviews: dict[str, _ToolObservation] = {}
    for item in observations:
        if item.index > final_test_index and item.tool in _GIT_REVIEW_TOOLS:
            final_reviews[item.tool] = item
    if set(final_reviews) != _GIT_REVIEW_TOOLS or any(
        item.result.outcome != "succeeded" for item in final_reviews.values()
    ):
        return "swebench_git_review_incomplete", "SWE-bench公开测试后缺少完整 Git 复核"
    return None


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


def _input_rate(config: SweBenchAgentRunConfig) -> int:
    price = config.price.input_price
    if not isinstance(price, FlatInputPrice):
        raise KernelError("swebench_cost_invalid", "SWE-bench输入价格合同无效")
    return rate_units(price.per_million)


def _unknown_request_reserve(config: SweBenchAgentRunConfig) -> int:
    return config.provider_config.max_request_bytes * _input_rate(
        config
    ) + config.provider_config.max_output_tokens * rate_units(config.price.output_per_million)


def _next_case_reserve(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchAgentRunConfig,
) -> int:
    known = plan.task_budget.max_tokens * max(
        _input_rate(config),
        rate_units(config.price.output_per_million),
    )
    return known + _unknown_request_reserve(config)


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
    path = root / _SUITE_STATE_FILE
    previous = read_suite_execution_state(path, plan) if path_present(path) else None
    rebuilt = build_swebench_suite_execution_state(
        plan,
        states,
        continue_quality_failures=(
            previous is None
            or previous.spec_version == "harnessix.swebench-suite-execution-state/v2"
        ),
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
    process_profile: ProductProcessProfile,
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
        process_profile=process_profile,
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
        suite = _reconcile_suite_state(root, plan, states)
        continue_quality_failures = (
            suite.spec_version == "harnessix.swebench-suite-execution-state/v2"
        )
        workspaces = await _materialize_all(
            plan,
            config,
            states,
            allow_network=allow_repository_network,
        )

        known_cost_units = 0
        reserved_cost_units = 0
        unaccounted_cases = 0
        completed_count = 0
        attempted_patches: list[str] = []
        for index, (case, workspace) in enumerate(zip(plan.cases, workspaces, strict=True)):
            token.checkpoint()
            state = states[index]
            terminal_quality_failure = continue_quality_failures and is_swebench_quality_failure(
                state
            )
            terminal_attempt = state.status == "completed" or terminal_quality_failure
            if state.status in {"failed", "cancelled"} and not terminal_quality_failure:
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
            patch = collect_workspace_patch(
                workspace,
                Path(config.git_executable),
                case.base_commit,
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
                _reconcile_suite_state(root, plan, states)
            if evidence_failure is not None and not (
                continue_quality_failures and is_swebench_quality_failure(state)
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

        suite = _reconcile_suite_state(root, plan, states)
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
            "quality_failed_cases": len(attempted_patches) - completed_count,
            "input_tokens": suite.input_tokens,
            "output_tokens": suite.output_tokens,
            "known_cost_currency": config.price.currency,
            "known_cost_amount": format_amount(known_cost_units),
            "reserved_cost_amount": format_amount(reserved_cost_units),
            "unaccounted_cost_cases": unaccounted_cases,
            "predictions_sha256": predictions_sha256,
        }
