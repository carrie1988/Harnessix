"""Task Pack Case经正式Agent、Trusted Action、Campaign与Suite端口执行。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never
from uuid import UUID, uuid5

from harnessix.agent.approvals import remaining_seconds
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    ItemStatus,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import get_turn
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, utc_now
from harnessix.evals.contracts import (
    CodingEvalEnvironment,
    CodingEvalReport,
    CodingEvalRunState,
    EvalTestObservation,
)
from harnessix.evals.execution_fs import (
    path_present,
)
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.report import (
    eval_report_sha256,
    read_eval_report,
    write_eval_report,
)
from harnessix.evals.run_state import read_eval_run_state, write_eval_run_state
from harnessix.evals.task_pack import (
    LoadedCodingEvalTaskPack,
    build_task_pack_product_profile,
)
from harnessix.evals.task_pack_contracts import CodingEvalTaskPackCase
from harnessix.evals.task_pack_materializer import (
    MaterializedCodingEvalTaskPackCase,
    materialize_task_pack_case,
)
from harnessix.models.contracts import ModelProvider
from harnessix.observability import NoOpObservability, Observability
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.product_config.action_runtime import open_default_product_action_runtime
from harnessix.product_config.workspace_patch_review import decode_workspace_patch_input
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import digest

TaskPackProviderFactory = Callable[
    [CodingEvalTaskPackCase, UUID], AbstractAsyncContextManager[ModelProvider]
]
Fault = Callable[[str], None]

_SESSION_FILE = "session.sqlite"
_RUN_STATE_FILE = "run-state.json"
_RUN_REPORT_FILE = "report.json"
_WORKSPACE_NAMESPACE = UUID("4a82127c-3d31-4c92-8981-9a1f47f2ff39")
_AUTOMATED_APPROVAL_ACTOR = "harnessix-eval-runner"


def _fault(_: str) -> None:
    return None


class _NoSecrets:
    """离线Task Pack Profile不允许解析任何Secret。"""

    def resolve(self, name: str) -> Never:
        raise KernelError("eval_task_pack_secret_denied", f"Task Pack禁止Secret：{name}")


@dataclass(frozen=True, slots=True)
class TaskPackCodingEvalResult:
    """一个Task Pack Trial的完整内部证据，不作为公开报告序列化。"""

    state: CodingEvalRunState
    report: CodingEvalReport
    turn: Turn
    workspace: Path


def _single_thread(sessions: SQLiteSessionStore, thread_ids: list[UUID]) -> UUID | None:
    if len(thread_ids) > 1:
        raise KernelError("eval_run_projection_invalid", "Task Pack Session包含多个Thread")
    return thread_ids[0] if thread_ids else None


def _request_turn(thread: Thread, request_id: str) -> Turn | None:
    matches = tuple(turn for turn in thread.turns if turn.request_id == request_id)
    if len(matches) > 1:
        raise KernelError("eval_run_projection_invalid", "Task Pack请求绑定多个Turn")
    if matches:
        return matches[0]
    if thread.turns:
        raise KernelError("eval_run_projection_invalid", "Task Pack Session包含非计划Turn")
    return None


def _pending_approval(turn: Turn) -> TrustedActionApprovalRequestContent | None:
    pending = tuple(
        item.content
        for item in turn.items
        if item.status is ItemStatus.STARTED
        and isinstance(item.content, TrustedActionApprovalRequestContent)
        and item.content.decision is None
    )
    if len(pending) > 1:
        raise KernelError("eval_approval_projection_invalid", "Task Pack存在多个待审批Action")
    return pending[0] if pending else None


def _approval_call(turn: Turn, approval: TrustedActionApprovalRequestContent) -> ToolCallContent:
    calls = tuple(
        item.content
        for item in turn.items
        if item.status is ItemStatus.COMPLETED
        and isinstance(item.content, ToolCallContent)
        and item.content.call_id == approval.call_id
    )
    if len(calls) != 1:
        raise KernelError("eval_approval_projection_invalid", "Task Pack审批缺少唯一工具调用")
    return calls[0]


def _require_allowed_approval(
    turn: Turn,
    approval: TrustedActionApprovalRequestContent,
    case: CodingEvalTaskPackCase,
) -> str:
    call = _approval_call(turn, approval)
    profile_tool = f"run_profile.{case.profile_id}"
    if call.tool == profile_tool:
        if approval.presentation != "process" or call.arguments != {
            "profile": case.profile_id,
            "selectors": [],
        }:
            raise KernelError("eval_approval_denied", "Task Pack只批准固定无参数检查Profile")
        return "profile"
    if call.tool != "apply_patch_batch" or approval.presentation != "patch_batch":
        raise KernelError("eval_approval_denied", "Task Pack只批准固定Profile和受限Workspace Patch")
    try:
        proposal = decode_workspace_patch_input(call.arguments)
    except KernelError:
        raise KernelError("eval_approval_denied", "Task Pack Patch参数不符合正式合同") from None
    paths = tuple(sorted(item.path for item in proposal.files))
    if (
        len(paths) > case.task.max_changed_files
        or not set(paths) <= set(case.task.allowed_changed_paths)
        or any(item.operation == "delete" for item in proposal.files)
    ):
        raise KernelError("eval_approval_denied", "Task Pack Patch超出任务允许变更边界")
    return "patch"


async def _await_cancel[T](operation: Awaitable[T], cancel: CancelToken) -> T:
    return await cancel.run(operation)


async def _drive_turn(
    runtime: AgentRuntime,
    sessions: SQLiteSessionStore,
    case: CodingEvalTaskPackCase,
    workspace: Path,
    run_id: UUID,
    cancel: CancelToken,
    fault: Fault,
) -> tuple[UUID, Turn]:
    thread_id = _single_thread(sessions, await sessions.thread_ids())
    if thread_id is None:
        thread = await runtime.create_thread(str(workspace))
        thread_id = thread.thread_id
        fault("task_pack_runner.after_thread")
    else:
        thread = await sessions.get_thread(thread_id)
        if thread.workspace != str(workspace):
            raise KernelError("eval_run_projection_invalid", "Task Pack Thread与Workspace不一致")
    request_id = f"coding-eval:{run_id}"
    turn = _request_turn(thread, request_id)
    if turn is None:
        turn = await _await_cancel(
            runtime.run_turn(
                thread_id,
                case.task.prompt,
                request_id=request_id,
                budget=case.task.budget,
            ),
            cancel,
        )
        fault("task_pack_runner.after_turn")
    assert turn is not None

    while turn.status not in TERMINAL_TURNS:
        cancel.checkpoint()
        if remaining_seconds(turn) <= 0:
            turn = await _await_cancel(runtime.resume_turn(thread_id, turn.turn_id), cancel)
            continue
        if turn.status is TurnStatus.WAITING_APPROVAL:
            approval = _pending_approval(turn)
            if approval is None:
                turn = await _await_cancel(runtime.resume_turn(thread_id, turn.turn_id), cancel)
                continue
            kind = _require_allowed_approval(turn, approval, case)
            turn = await _await_cancel(
                runtime.reply_approval(
                    thread_id,
                    turn.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED,
                        actor=_AUTOMATED_APPROVAL_ACTOR,
                        reason="Task Pack固定边界内的自动评测审批",
                    ),
                ),
                cancel,
            )
            fault(f"task_pack_runner.after_{kind}_approval")
            turn = await _await_cancel(runtime.resume_turn(thread_id, turn.turn_id), cancel)
            continue
        if turn.status in {
            TurnStatus.ACCEPTED,
            TurnStatus.EXECUTING_TOOLS,
            TurnStatus.WAITING_ACTION,
        }:
            turn = await _await_cancel(runtime.resume_turn(thread_id, turn.turn_id), cancel)
            continue
        raise KernelError("eval_run_projection_invalid", "Task Pack Turn停留在不可恢复状态")
    return thread_id, turn


def _profile_observation(
    result: ToolResultContent,
    profile_id: str,
    phase: Literal["baseline", "final"],
) -> EvalTestObservation | None:
    output = result.output
    if not isinstance(output, dict) or output.get("profile") != profile_id:
        return None
    state = output.get("state")
    stop_reason = output.get("stop_reason")
    returncode = output.get("returncode")
    if state != "exited" or stop_reason != "exited" or type(returncode) is not int:
        return None
    evidence = {
        "profile": profile_id,
        "state": state,
        "stop_reason": stop_reason,
        "returncode": returncode,
        "stdout": output.get("stdout"),
        "stderr": output.get("stderr"),
        "complete": output.get("complete"),
    }
    return EvalTestObservation(
        check_id=profile_id,
        phase=phase,
        passed=returncode == 0,
        returncode=returncode,
        output_sha256=digest(evidence),
        elapsed_seconds=0,
    )


def _profile_observations(
    turn: Turn,
    case: CodingEvalTaskPackCase,
) -> tuple[tuple[EvalTestObservation, ...], tuple[EvalTestObservation, ...]]:
    calls = {
        item.content.call_id: item.content
        for item in turn.items
        if item.status is ItemStatus.COMPLETED and isinstance(item.content, ToolCallContent)
    }
    results: list[ToolResultContent] = []
    profile_tool = f"run_profile.{case.profile_id}"
    for item in turn.items:
        content = item.content
        if item.status is not ItemStatus.COMPLETED or not isinstance(content, ToolResultContent):
            continue
        call = calls.get(content.call_id)
        if call is not None and call.tool == profile_tool:
            results.append(content)
    if not results:
        raise KernelError("eval_baseline_missing", "Task Pack Turn缺少固定Profile基线证据")
    baseline = _profile_observation(results[0], case.profile_id, "baseline")
    if baseline is None or baseline.passed:
        raise KernelError("eval_baseline_invalid", "Task Pack固定Profile基线未按任务失败")
    final = (
        _profile_observation(results[-1], case.profile_id, "final") if len(results) > 1 else None
    )
    return (baseline,), (() if final is None else (final,))


def _require_completed_trial(
    state: CodingEvalRunState,
    report: CodingEvalReport,
    turn: Turn,
    case: CodingEvalTaskPackCase,
    environment: CodingEvalEnvironment,
    expected_run_id: UUID,
) -> None:
    repository = case.task.repository
    observed = (
        state.status,
        state.run_id,
        report.run_id,
        state.task_id,
        report.task_id,
        state.task_version,
        report.task_version,
        state.task_fingerprint,
        report.task_fingerprint,
        state.baseline_revision,
        state.baseline_tree_sha256,
        report.git.baseline_revision,
        report.git.baseline_tree_sha256,
        state.environment,
        report.environment,
        state.thread_id is not None,
        state.turn_id,
        state.report_sha256,
        state.baseline_observations,
        report.started_at,
    )
    expected = (
        "completed",
        expected_run_id,
        expected_run_id,
        case.task.task_id,
        case.task.task_id,
        case.task.task_version,
        case.task.task_version,
        case.task.fingerprint,
        case.task.fingerprint,
        repository.source_revision,
        repository.baseline_tree_sha256,
        repository.source_revision,
        repository.baseline_tree_sha256,
        environment,
        environment,
        True,
        turn.turn_id,
        eval_report_sha256(report),
        report.baseline_observations,
        state.started_at,
    )
    if observed != expected:
        raise KernelError("eval_run_mismatch", "Task Pack运行状态、报告或Session不一致")


async def _load_completed_run(
    run_root: Path,
    case: CodingEvalTaskPackCase,
    environment: CodingEvalEnvironment,
    expected_run_id: UUID,
) -> TaskPackCodingEvalResult:
    state = read_eval_run_state(run_root / _RUN_STATE_FILE)
    report = read_eval_report(run_root / _RUN_REPORT_FILE)
    if state.thread_id is None or state.turn_id is None:
        raise KernelError("eval_run_mismatch", "Task Pack完成状态缺少Thread或Turn")
    sessions = SQLiteSessionStore(run_root / _SESSION_FILE)
    turn = get_turn(await sessions.get_thread(state.thread_id), state.turn_id)
    _require_completed_trial(state, report, turn, case, environment, expected_run_id)
    return TaskPackCodingEvalResult(state, report, turn, run_root / "workspace")


async def _completed_session_turn(
    run_root: Path,
    workspace: Path,
    run_id: UUID,
) -> tuple[UUID, Turn] | None:
    """从持久Session识别已完成Turn，避免报告窗口恢复时重新打开Provider。"""

    if not path_present(run_root / _SESSION_FILE):
        return None
    sessions = SQLiteSessionStore(run_root / _SESSION_FILE)
    thread_id = _single_thread(sessions, await sessions.thread_ids())
    if thread_id is None:
        return None
    thread = await sessions.get_thread(thread_id)
    if thread.workspace != str(workspace):
        raise KernelError("eval_run_projection_invalid", "Task Pack Thread与Workspace不一致")
    turn = _request_turn(thread, f"coding-eval:{run_id}")
    if turn is None or turn.status not in TERMINAL_TURNS:
        return None
    return thread_id, turn


def _review_findings(case: CodingEvalTaskPackCase) -> tuple[str, ...]:
    oracle = case.review_oracle
    return () if oracle is None else tuple(item.finding_id for item in oracle.required_findings)


async def _run_agent(
    loaded: LoadedCodingEvalTaskPack,
    materialized: MaterializedCodingEvalTaskPackCase,
    git_executable: Path,
    container_engine: Path,
    provider_factory: TaskPackProviderFactory,
    token: CancelToken,
    observer: Observability,
    fail: Fault,
) -> tuple[UUID, Turn]:
    case = materialized.case
    sessions = SQLiteSessionStore(materialized.run_root / _SESSION_FILE)
    artifacts = SQLiteArtifactStore(sessions)
    profile = build_task_pack_product_profile(loaded, case.profile_id, container_engine)
    async with provider_factory(case, materialized.manifest.run_id) as provider:
        async with CodingToolRuntime(
            materialized.workspace,
            artifacts=artifacts,
            git_executable=git_executable,
        ) as tools:
            async with open_default_product_action_runtime(
                materialized.run_root,
                materialized.workspace,
                artifacts,
                _NoSecrets(),
                build_product_action_config(process_profiles=(profile,)),
                artifact_workspace_scope=tools.workspace_scope,
            ) as actions:
                if actions.gateway is None:
                    raise KernelError(
                        "eval_task_pack_action_unavailable",
                        "Task Pack固定Profile或Workspace Patch能力不可用",
                    )
                async with AgentRuntime(
                    sessions,
                    provider,
                    scoped_tools=tools,
                    trusted_actions=actions.gateway,
                    artifacts=artifacts,
                    observability=observer,
                    fault=fail,
                ) as runtime:
                    return await _drive_turn(
                        runtime,
                        sessions,
                        case,
                        materialized.workspace,
                        materialized.manifest.run_id,
                        token,
                        fail,
                    )


async def _grade_and_publish(
    materialized: MaterializedCodingEvalTaskPackCase,
    git_executable: Path,
    environment: CodingEvalEnvironment,
    thread_id: UUID,
    turn: Turn,
    token: CancelToken,
    fail: Fault,
) -> TaskPackCodingEvalResult:
    case = materialized.case
    baseline, final = _profile_observations(turn, case)
    git = await collect_git_evidence(
        materialized.workspace,
        git_executable,
        baseline_revision=materialized.manifest.baseline_revision,
        baseline_tree_sha256=materialized.manifest.baseline_tree_sha256,
        cancel=token,
    )
    completed_at = turn.completed_at or utc_now()
    report = grade_coding_eval(
        case.task,
        turn,
        run_id=materialized.manifest.run_id,
        environment=environment,
        started_at=turn.created_at,
        completed_at=completed_at,
        baseline_observations=baseline,
        final_observations=final,
        git=git,
        required_review_finding_ids=_review_findings(case),
    )
    report_path = materialized.run_root / _RUN_REPORT_FILE
    if path_present(report_path):
        if read_eval_report(report_path) != report:
            raise KernelError("eval_report_mismatch", "Task Pack已发布报告与Session证据不一致")
    else:
        write_eval_report(report_path, report)
        fail("task_pack_runner.after_report")
    state = CodingEvalRunState(
        run_id=materialized.manifest.run_id,
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        baseline_revision=materialized.manifest.baseline_revision,
        baseline_tree_sha256=materialized.manifest.baseline_tree_sha256,
        execution_workspace_id=uuid5(
            _WORKSPACE_NAMESPACE,
            str(materialized.manifest.run_id),
        ),
        status="completed",
        thread_id=thread_id,
        turn_id=turn.turn_id,
        baseline_observations=baseline,
        environment=environment,
        report_sha256=eval_report_sha256(report),
        started_at=turn.created_at,
        updated_at=completed_at,
    )
    write_eval_run_state(materialized.run_root / _RUN_STATE_FILE, state)
    return TaskPackCodingEvalResult(state, report, turn, materialized.workspace)


async def run_task_pack_coding_eval(
    loaded: LoadedCodingEvalTaskPack,
    runs_root: Path,
    git_executable: Path,
    container_engine: Path,
    case_id: str,
    run_id: UUID,
    provider_factory: TaskPackProviderFactory,
    environment: CodingEvalEnvironment,
    cancel: CancelToken | None = None,
    *,
    observability: Observability | None = None,
    fault: Fault | None = None,
) -> TaskPackCodingEvalResult:
    """执行或从Session事实恢复一个Task Pack Trial；完成后才发布标准Run状态。"""

    token = cancel or CancelToken()
    fail = fault or _fault
    materialized = materialize_task_pack_case(
        loaded,
        runs_root,
        git_executable,
        case_id,
        run_id,
    )
    case = materialized.case
    run_root = materialized.run_root
    state_path = run_root / _RUN_STATE_FILE
    report_path = run_root / _RUN_REPORT_FILE
    if path_present(state_path):
        if not path_present(report_path):
            raise KernelError("eval_report_mismatch", "Task Pack完成状态缺少报告")
        return await _load_completed_run(run_root, case, environment, run_id)
    recovered = await _completed_session_turn(run_root, materialized.workspace, run_id)
    if recovered is None:
        thread_id, turn = await _run_agent(
            loaded,
            materialized,
            git_executable,
            container_engine,
            provider_factory,
            token,
            observability or NoOpObservability(),
            fail,
        )
    else:
        thread_id, turn = recovered
    return await _grade_and_publish(
        materialized,
        git_executable,
        environment,
        thread_id,
        turn,
        token,
        fail,
    )
