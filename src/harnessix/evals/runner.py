"""内置历史任务经正式Agent、审批、Worker与评分链路的可信编排。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

from harnessix.agent.approvals import remaining_seconds
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    PatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    ToolCallContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import get_turn, pending_calls
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, Principal, utc_now
from harnessix.domain.registry import ToolRegistry
from harnessix.evals.catalog import HistoricalCodingEval
from harnessix.evals.checks import (
    historical_check_arguments,
    historical_python_launcher,
    run_historical_checks,
)
from harnessix.evals.contracts import (
    CodingEvalEnvironment,
    CodingEvalReport,
    CodingEvalRunState,
)
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.materializer import (
    MaterializedCodingEval,
    materialize_historical_coding_eval,
)
from harnessix.evals.report import MAX_EVAL_REPORT_BYTES, read_eval_report, write_eval_report
from harnessix.evals.run_state import read_eval_run_state, write_eval_run_state
from harnessix.models.contracts import ModelProvider
from harnessix.observability import NoOpObservability, Observability
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.managed import ManagedPatchWorkspace, PatchWorkspaces
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.processes.test_contracts import TestProfile
from harnessix.processes.test_profiles import RunTestsAgentBridge
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace
from harnessix.worker import ActionWorker

_STATE_FILE = "run-state.json"
_REPORT_FILE = "report.json"
_MANAGED_DIRECTORY = "managed"
_SESSION_FILE = "session.sqlite"
_EFFECT_FILE = "effects.sqlite"
_MAX_TREE_PATH_BYTES = 4 * 1024 * 1024
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


@dataclass(frozen=True, slots=True)
class HistoricalCodingEvalResult:
    state: CodingEvalRunState
    report: CodingEvalReport
    workspace: Path


def _fault(_: str) -> None:
    return None


def _report_digest(path: Path) -> str:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_EVAL_REPORT_BYTES:
            raise OSError
        value = hashlib.sha256()
        remaining = MAX_EVAL_REPORT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            value.update(chunk)
            remaining -= len(chunk)
        if info.st_size != MAX_EVAL_REPORT_BYTES + 1 - remaining:
            raise OSError
        return value.hexdigest()
    except OSError:
        raise KernelError("eval_report_invalid", "Eval报告缺失、损坏或超过上限") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _tracked_paths(materialized: MaterializedCodingEval, git_executable: Path) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            (
                str(git_executable.resolve(strict=True)),
                "--no-pager",
                "--no-optional-locks",
                "-c",
                "core.hooksPath=/dev/null",
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                "HEAD",
            ),
            cwd=materialized.workspace,
            env=_GIT_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        body = result.stdout
        if result.returncode != 0 or not 1 <= len(body) <= _MAX_TREE_PATH_BYTES:
            raise ValueError
        decoded = body.decode("utf-8", errors="strict")
        paths = tuple(decoded.removesuffix("\0").split("\0"))
        if (
            len(paths) != materialized.manifest.tracked_files
            or paths != tuple(sorted(set(paths)))
            or any(
                not path
                or PurePosixPath(path).is_absolute()
                or any(part in {"", ".", "..", ".git"} for part in PurePosixPath(path).parts)
                for path in paths
            )
        ):
            raise ValueError
        return paths
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise KernelError(
            "eval_execution_workspace_failed", "Eval受管执行副本的来源清单无效"
        ) from None


async def _provision(
    definition: HistoricalCodingEval,
    materialized: MaterializedCodingEval,
    git_executable: Path,
    python_executable: Path,
    environment: CodingEvalEnvironment,
    cancel: CancelToken,
    started_at: datetime,
) -> CodingEvalRunState:
    task = definition.task
    baseline = await run_historical_checks(
        definition, materialized, python_executable, "baseline", cancel
    )
    if tuple(item.check_id for item in baseline) != task.baseline_checks or any(
        item.passed for item in baseline
    ):
        raise KernelError("eval_baseline_invalid", "Eval历史缺陷基线未按任务定义失败")
    clean = await collect_git_evidence(
        materialized.workspace,
        git_executable,
        baseline_revision=materialized.manifest.baseline_revision,
        baseline_tree_sha256=materialized.manifest.baseline_tree_sha256,
        cancel=cancel,
    )
    if (
        clean.head_revision != clean.baseline_revision
        or clean.changed_paths
        or clean.staged_paths
        or clean.untracked_paths
        or clean.unsupported_change_paths
    ):
        raise KernelError("eval_materialization_dirty", "Eval物化基线在执行副本创建前已变更")
    managed_root = materialized.run_root / _MANAGED_DIRECTORY
    if managed_root.exists() or managed_root.is_symlink():
        raise KernelError("eval_run_state_invalid", "Eval运行缺少状态但已有受管副本")
    paths = _tracked_paths(materialized, git_executable)
    with Workspace(materialized.workspace) as source:
        managed_paths: list[str] = []
        host_only_paths: list[str] = []
        for path in paths:
            try:
                source.parts(path)
            except ReadToolError as error:
                if error.code != "path_denied":
                    raise
                host_only_paths.append(path)
            else:
                managed_paths.append(path)
        if tuple(host_only_paths) != definition.host_only_paths:
            raise KernelError(
                "eval_execution_workspace_failed",
                "Eval工具隐藏路径与任务固定定义不一致",
            )
        factory = PatchWorkspaces(managed_root)
        copy = factory.create(source, managed_paths, ReadOperation())
    with copy:
        try:
            for path in host_only_paths:
                source_path = materialized.workspace / path
                target_path = copy.workspace.root / path
                if not source_path.is_file() or source_path.is_symlink():
                    raise OSError
                target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path, follow_symlinks=False)
            shutil.copytree(
                materialized.workspace / ".git",
                copy.workspace.root / ".git",
                symlinks=True,
            )
        except OSError:
            raise KernelError(
                "eval_execution_workspace_failed", "Eval受管执行副本Git基线创建失败"
            ) from None
        copied = await collect_git_evidence(
            copy.workspace.root,
            git_executable,
            baseline_revision=materialized.manifest.baseline_revision,
            baseline_tree_sha256=materialized.manifest.baseline_tree_sha256,
            cancel=cancel,
        )
        if (
            copied.head_revision != materialized.manifest.baseline_revision
            or copied.changed_paths
            or copied.staged_paths
            or copied.untracked_paths
            or copied.unsupported_change_paths
        ):
            raise KernelError("eval_execution_workspace_failed", "Eval受管执行副本与物化基线不一致")
        return CodingEvalRunState(
            run_id=materialized.manifest.run_id,
            task_id=task.task_id,
            task_version=task.task_version,
            task_fingerprint=task.fingerprint,
            baseline_revision=materialized.manifest.baseline_revision,
            baseline_tree_sha256=materialized.manifest.baseline_tree_sha256,
            execution_workspace_id=copy.workspace_id,
            status="ready",
            baseline_observations=baseline,
            environment=environment,
            started_at=started_at,
            updated_at=utc_now(),
        )


def _require_state(
    state: CodingEvalRunState,
    definition: HistoricalCodingEval,
    materialized: MaterializedCodingEval,
    environment: CodingEvalEnvironment,
) -> None:
    task = definition.task
    if (
        state.run_id != materialized.manifest.run_id
        or state.task_id != task.task_id
        or state.task_version != task.task_version
        or state.task_fingerprint != task.fingerprint
        or state.baseline_revision != materialized.manifest.baseline_revision
        or state.baseline_tree_sha256 != materialized.manifest.baseline_tree_sha256
        or state.environment != environment
        or tuple(item.check_id for item in state.baseline_observations) != task.baseline_checks
        or any(item.passed for item in state.baseline_observations)
    ):
        raise KernelError("eval_run_mismatch", "Eval运行状态与任务、物化基线或环境不一致")


def _require_report(
    report: CodingEvalReport,
    state: CodingEvalRunState,
    definition: HistoricalCodingEval,
) -> None:
    task = definition.task
    if (
        report.run_id != state.run_id
        or report.task_id != task.task_id
        or report.task_version != task.task_version
        or report.task_fingerprint != task.fingerprint
        or report.environment != state.environment
        or report.started_at != state.started_at
        or report.baseline_observations != state.baseline_observations
        or report.git.baseline_revision != state.baseline_revision
        or report.git.baseline_tree_sha256 != state.baseline_tree_sha256
    ):
        raise KernelError("eval_report_mismatch", "Eval报告与运行状态不一致")


def _pending_approval(turn: Turn) -> PatchApprovalRequestContent | ProcessApprovalRequestContent:
    values = [
        item.content
        for item in turn.items
        if item.status is ItemStatus.STARTED
        and isinstance(item.content, PatchApprovalRequestContent | ProcessApprovalRequestContent)
        and item.content.decision is None
    ]
    if len(values) != 1:
        raise KernelError("eval_approval_projection_invalid", "Eval Turn待审批投影无唯一请求")
    return values[0]


def _require_allowed_approval(
    turn: Turn,
    approval: PatchApprovalRequestContent | ProcessApprovalRequestContent,
    definition: HistoricalCodingEval,
) -> str:
    calls = [
        item.content
        for item in turn.items
        if item.status is ItemStatus.COMPLETED
        and isinstance(item.content, ToolCallContent)
        and item.content.call_id == approval.call_id
    ]
    if len(calls) != 1:
        raise KernelError("eval_approval_projection_invalid", "Eval审批缺少唯一工具调用")
    call = calls[0]
    if isinstance(approval, ProcessApprovalRequestContent):
        profile = call.arguments.get("profile") if isinstance(call.arguments, dict) else None
        if (
            call.tool != "run_tests"
            or set(call.arguments) != {"profile"}
            or profile not in definition.task.required_test_profiles
        ):
            raise KernelError("eval_approval_denied", "Eval只批准任务声明的测试Profile")
        return "process"
    if (
        call.tool != "apply_patch"
        or approval.plan.manifest.path not in definition.task.allowed_changed_paths
    ):
        raise KernelError("eval_approval_denied", "Eval只批准任务允许路径的受管Patch")
    return "patch"


async def _await_cancel[T](operation: Awaitable[T], cancel: CancelToken) -> T:
    return await cancel.run(operation)


async def _drive_turn(
    runtime: AgentRuntime,
    worker: ActionWorker,
    definition: HistoricalCodingEval,
    state: CodingEvalRunState,
    state_path: Path,
    execution_root: Path,
    cancel: CancelToken,
    fault: Callable[[str], None],
) -> tuple[Turn, CodingEvalRunState]:
    task = definition.task
    assert state.thread_id is not None
    thread_id = state.thread_id
    thread = await runtime.store.get_thread(thread_id)
    if thread.workspace != str(execution_root):
        raise KernelError("eval_run_projection_invalid", "Eval Thread与执行副本不一致")
    request_id = f"coding-eval:{state.run_id}"
    if state.turn_id is None:
        turn = await _await_cancel(
            runtime.run_turn(
                thread_id,
                task.prompt,
                request_id=request_id,
                budget=task.budget,
            ),
            cancel,
        )
        state = state.model_copy(update={"turn_id": turn.turn_id, "updated_at": utc_now()})
        write_eval_run_state(state_path, state)
        fault("eval_runner.after_turn_bound")
    else:
        turn = get_turn(thread, state.turn_id)

    while turn.status not in {
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
        TurnStatus.INTERRUPTED,
    }:
        cancel.checkpoint()
        if remaining_seconds(turn) <= 0:
            turn = await _await_cancel(runtime.resume_turn(thread_id, turn.turn_id), cancel)
            continue
        if turn.status is TurnStatus.WAITING_APPROVAL:
            approval = _pending_approval(turn)
            kind = _require_allowed_approval(turn, approval, definition)
            turn = await _await_cancel(
                runtime.reply_approval(
                    thread_id,
                    turn.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED,
                        actor="harnessix-eval-runner",
                        reason="内置任务允许边界内的自动评测审批",
                    ),
                ),
                cancel,
            )
            fault(f"eval_runner.after_{kind}_approval")
            if kind == "patch":
                turn = await _await_cancel(runtime.resume_turn(thread_id, turn.turn_id), cancel)
            continue
        if turn.status is TurnStatus.WAITING_ACTION:
            calls = pending_calls(turn)
            if len(calls) != 1 or calls[0].tool != "run_tests":
                raise KernelError("eval_run_projection_invalid", "Eval仅允许等待固定测试Action")
            processed = await _await_cancel(worker.run_once(), cancel)
            if processed is not None:
                matching = [
                    item.content
                    for item in turn.items
                    if isinstance(item.content, ProcessApprovalRequestContent)
                    and item.content.call_id == calls[0].call_id
                ]
                if len(matching) != 1 or processed.request.action_id != matching[0].plan.action_id:
                    raise KernelError(
                        "eval_action_worker_mismatch", "Action Worker消费了非本次Eval测试Action"
                    )
                fault("eval_runner.after_process_worker")
            else:
                await worker.service.journal.recover_expired()
                await _await_cancel(asyncio.sleep(0.05), cancel)
            turn = await _await_cancel(runtime.resume_turn(thread_id, turn.turn_id), cancel)
            continue
        raise KernelError("eval_run_projection_invalid", "Eval Turn停留在不可恢复的中间状态")
    return turn, state


async def run_historical_coding_eval(
    source_root: Path,
    runs_root: Path,
    git_executable: Path,
    python_executable: Path,
    definition: HistoricalCodingEval,
    run_id: UUID,
    provider: ModelProvider,
    environment: CodingEvalEnvironment,
    cancel: CancelToken | None = None,
    *,
    observability: Observability | None = None,
    fault: Callable[[str], None] | None = None,
) -> HistoricalCodingEvalResult:
    """运行或从持久审批边界恢复一个内置历史任务；Provider生命周期由调用方持有。"""

    token = cancel or CancelToken()
    fail = fault or _fault
    invocation_started_at = utc_now()
    if (
        len(definition.task.required_test_profiles) != 1
        or len(definition.task.behavior_checks) != 1
    ):
        raise KernelError("eval_task_unsupported", "历史任务运行器当前要求唯一可见测试Profile")
    materialized = materialize_historical_coding_eval(
        source_root, runs_root, git_executable, definition, run_id
    )
    state_path = materialized.run_root / _STATE_FILE
    report_path = materialized.run_root / _REPORT_FILE
    if state_path.exists() or state_path.is_symlink():
        state = read_eval_run_state(state_path)
    else:
        state = await _provision(
            definition,
            materialized,
            git_executable,
            python_executable,
            environment,
            token,
            invocation_started_at,
        )
        write_eval_run_state(state_path, state)
        fail("eval_runner.after_ready")
    _require_state(state, definition, materialized, environment)
    factory = PatchWorkspaces(materialized.run_root / _MANAGED_DIRECTORY)
    copy: ManagedPatchWorkspace = factory.open(state.execution_workspace_id)
    execution_root = copy.workspace.root

    if state.status == "completed":
        try:
            report = read_eval_report(report_path)
            _require_report(report, state, definition)
            if _report_digest(report_path) != state.report_sha256:
                raise KernelError("eval_report_mismatch", "Eval报告摘要与运行状态不一致")
            return HistoricalCodingEvalResult(state=state, report=report, workspace=execution_root)
        finally:
            copy.close()
    if report_path.exists() or report_path.is_symlink():
        try:
            report = read_eval_report(report_path)
            _require_report(report, state, definition)
            if state.turn_id is None:
                raise KernelError("eval_report_mismatch", "Eval报告缺少已绑定Turn")
            state = state.model_copy(
                update={
                    "status": "completed",
                    "report_sha256": _report_digest(report_path),
                    "updated_at": utc_now(),
                }
            )
            write_eval_run_state(state_path, state)
            return HistoricalCodingEvalResult(state=state, report=report, workspace=execution_root)
        finally:
            copy.close()

    observer = observability or NoOpObservability()
    actions: ActionService | None = None
    try:
        launcher = historical_python_launcher(materialized, python_executable)
        registry = ToolRegistry()
        registry.register(
            process_action_tool(lambda: HostProcessRuntime(execution_root, {"python": launcher}))
        )
        actions = ActionService(
            journal=SQLiteEffectJournal(materialized.run_root / _EFFECT_FILE),
            registry=registry,
            policy_engine=DefaultPolicyEngine(),
            lease_seconds=5,
            auto_execute=False,
            observability=observer,
        )
        await actions.initialize()
        sessions = SQLiteSessionStore(materialized.run_root / _SESSION_FILE)
        artifacts = SQLiteArtifactStore(sessions)
        processes = RunTestsAgentBridge(
            actions,
            Principal(
                tenant_id="coding-eval",
                subject_id=definition.task.task_id,
                framework="harnessix-agent",
            ),
            execution_root,
            (
                TestProfile(
                    name=definition.task.required_test_profiles[0],
                    description="内置历史缺陷行为检查",
                    program="python",
                    arguments=historical_check_arguments(
                        execution_root, definition.check(definition.task.behavior_checks[0]).mode
                    ),
                    timeout_seconds=60,
                ),
            ),
        )
        worker = ActionWorker(
            actions,
            poll_seconds=0.01,
            heartbeat_seconds=1,
            recovery_interval_seconds=0.1,
        )
    except BaseException:
        copy.close()
        if actions is not None:
            await actions.close()
        raise
    assert actions is not None
    try:
        with copy:
            async with (
                ManagedPatchBridge(copy) as patches,
                CodingToolRuntime(
                    execution_root,
                    artifacts=artifacts,
                    git_executable=git_executable,
                ) as tools,
            ):
                publisher = SQLiteProcessArtifactPublisher(
                    artifacts, processes, workspace_scope=tools.workspace_scope
                )
                async with AgentRuntime(
                    sessions,
                    provider,
                    scoped_tools=tools,
                    artifacts=artifacts,
                    patches=patches,
                    processes=processes,
                    process_artifacts=publisher,
                    observability=observer,
                ) as runtime:
                    if state.thread_id is None:
                        existing = await sessions.thread_ids()
                        if len(existing) > 1:
                            raise KernelError(
                                "eval_run_projection_invalid", "Eval Session包含多个Thread"
                            )
                        thread = (
                            await sessions.get_thread(existing[0])
                            if existing
                            else await runtime.create_thread(str(execution_root))
                        )
                        if thread.workspace != str(execution_root) or thread.turns:
                            raise KernelError(
                                "eval_run_projection_invalid", "Eval ready状态与Session事实不一致"
                            )
                        state = state.model_copy(
                            update={
                                "status": "running",
                                "thread_id": thread.thread_id,
                                "updated_at": utc_now(),
                            }
                        )
                        write_eval_run_state(state_path, state)
                        fail("eval_runner.after_thread_bound")
                    else:
                        ids = await sessions.thread_ids()
                        if ids != [state.thread_id]:
                            raise KernelError(
                                "eval_run_projection_invalid", "Eval状态与Session Thread索引不一致"
                            )
                    turn, state = await _drive_turn(
                        runtime,
                        worker,
                        definition,
                        state,
                        state_path,
                        execution_root,
                        token,
                        fail,
                    )
            final = await run_historical_checks(
                definition,
                materialized,
                python_executable,
                "final",
                token,
                workspace=execution_root,
            )
            git = await collect_git_evidence(
                execution_root,
                git_executable,
                baseline_revision=state.baseline_revision,
                baseline_tree_sha256=state.baseline_tree_sha256,
                cancel=token,
            )
            completed_at = utc_now()
            report = grade_coding_eval(
                definition.task,
                turn,
                run_id=run_id,
                environment=environment,
                started_at=state.started_at,
                completed_at=completed_at,
                baseline_observations=state.baseline_observations,
                final_observations=final,
                git=git,
            )
            write_eval_report(report_path, report)
            fail("eval_runner.after_report")
            state = state.model_copy(
                update={
                    "status": "completed",
                    "report_sha256": _report_digest(report_path),
                    "updated_at": completed_at,
                }
            )
            write_eval_run_state(state_path, state)
            return HistoricalCodingEvalResult(
                state=state,
                report=report,
                workspace=execution_root,
            )
    finally:
        await actions.close()
