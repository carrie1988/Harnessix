"""用正式 AgentRuntime 执行单个预注册 SWE-bench Case。"""

from __future__ import annotations

from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Never
from uuid import UUID

from harnessix.agent.approvals import remaining_seconds
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    Budget,
    ItemStatus,
    Thread,
    ToolCallContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL, workspace_patch_descriptor
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.evals.swebench.contracts import (
    SweBenchPreRegistrationPlan,
    SweBenchPublicCase,
)
from harnessix.evals.task_pack_publication import (
    TaskPackHistoryReadControl,
    TaskPackPublicationOwner,
    open_task_pack_publication,
    provider_publication_scope,
)
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelProvider
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.observability import NoOpObservability, Observability
from harnessix.product_config.action_contracts import (
    ProductProcessProfile,
    build_product_action_config,
)
from harnessix.product_config.action_runtime import open_default_product_action_runtime
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from harnessix.product_config.process_action import (
    decode_run_profile,
    process_profile_descriptor,
)
from harnessix.product_config.process_profile import process_tool_name
from harnessix.product_config.workspace_patch_review import decode_workspace_patch_input
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway

_AUTOMATED_APPROVAL_ACTOR = "harnessix-swebench-runner"
_PROMPT_VERSION = "harnessix.swebench-agent-prompt/v5"


class _NoSecrets:
    """SWE-bench 工具侧禁止解析任何 Secret。"""

    def resolve(self, name: str) -> Never:
        raise KernelError("swebench_tool_secret_denied", f"SWE-bench 工具禁止Secret：{name}")


def build_swebench_agent_prompt(case: SweBenchPublicCase, budget: Budget | None = None) -> str:
    """只向 Agent 提供预注册公开问题，不暴露 Task Repo 或评分信息。"""

    budget_note = (
        ""
        if budget is None
        else (
            f"Hard task limits: {budget.max_tokens} cumulative input-plus-output tokens across "
            f"all requests, {budget.max_steps} model steps, {budget.timeout_seconds:g} seconds. "
            "Repeated history and tool catalogs count again on every request; this is not a "
            "per-request token allowance. Plan investigation, patching, tests, possible repair "
            "and final review within that same total. Avoid repeated equivalent no-match searches; "
            "follow the public call chain or named symbols instead.\n"
        )
    )
    return (
        "You are solving one SWE-bench task in the current repository.\n"
        f"{budget_note}"
        "Work autonomously and leave the working tree with the smallest correct patch. Start from "
        "the named symbols and files; use focused grep/read calls rather than broad exploration. "
        "Batch independent source reads in one response when supported; write and test in order. "
        "Locate the exact named definition before editing, rather than a similarly named method. "
        "Use narrow 60-80 line read windows and reserve budget for validation and final review. "
        "For existing files use exact edit operations with "
        "unique old_text/new_text anchors; never replace an entire file with an excerpt. After "
        "patching, select focused relative test targets or repository-native test labels that "
        "you have actually observed in the public repository. Never guess an exact test node; "
        "when only the file is known, run that file. Then run the public-tests profile. If a test "
        "fails, "
        "diagnose the failure, repair the patch, "
        "and rerun the public-tests profile; the latest patched state must be retested before "
        "finishing. After the final test succeeds, inspect the forced git_review status and diff "
        "evidence. Do not ask questions or use network "
        "access, hidden tests, reference patches, hints, or evaluator artifacts.\n\n"
        f"Instance: {case.instance_id}\n"
        f"Repository: {case.repo}\n"
        f"Base commit: {case.base_commit}\n\n"
        "Problem statement:\n"
        f"{case.problem_statement}"
    )


def _request_turn(thread: Thread, request_id: str) -> Turn | None:
    if not thread.turns:
        return None
    if len(thread.turns) != 1 or thread.turns[0].request_id != request_id:
        raise KernelError("swebench_run_projection_invalid", "SWE-bench Session包含非计划Turn")
    return thread.turns[0]


def _pending_approval(turn: Turn) -> TrustedActionApprovalRequestContent | None:
    pending = tuple(
        item.content
        for item in turn.items
        if item.status is ItemStatus.STARTED
        and isinstance(item.content, TrustedActionApprovalRequestContent)
        and item.content.decision is None
    )
    if len(pending) > 1:
        raise KernelError("swebench_approval_invalid", "SWE-bench存在多个待审批Action")
    return pending[0] if pending else None


def _approval_call(turn: Turn, approval: TrustedActionApprovalRequestContent) -> ToolCallContent:
    matches = tuple(
        item.content
        for item in turn.items
        if item.status is ItemStatus.COMPLETED
        and isinstance(item.content, ToolCallContent)
        and item.content.call_id == approval.call_id
    )
    if len(matches) != 1:
        raise KernelError("swebench_approval_invalid", "SWE-bench审批缺少唯一工具调用")
    return matches[0]


def _approval_decision(
    turn: Turn,
    approval: TrustedActionApprovalRequestContent,
    process_profile: ProductProcessProfile,
) -> ApprovalDecision:
    """只批准正式 Patch 或当前 Case 唯一公开测试 Profile。"""

    call = _approval_call(turn, approval)
    patch_descriptor = workspace_patch_descriptor()
    approved = (
        call.tool == patch_descriptor.name
        and call.tool_version == patch_descriptor.version
        and call.effect_class == patch_descriptor.effect_class
        and call.requires_approval
        and approval.presentation == "patch_batch"
    )
    if approved:
        try:
            decode_workspace_patch_input(call.arguments)
        except KernelError:
            approved = False
    descriptor = process_profile_descriptor(process_profile)
    if (
        call.tool == descriptor.name
        and call.tool_version == descriptor.version
        and call.effect_class == descriptor.effect_class
        and call.requires_approval
        and approval.presentation == "process"
    ):
        try:
            decode_run_profile(
                process_profile.profile_id,
                process_profile.selector_policy,
                call.arguments,
            )
        except ValueError:
            approved = False
        else:
            approved = True
    return ApprovalDecision(
        outcome=ApprovalOutcome.APPROVED if approved else ApprovalOutcome.REJECTED,
        actor=_AUTOMATED_APPROVAL_ACTOR,
        reason=(
            "SWE-bench隔离Workspace内的受控Patch或公开测试Profile"
            if approved
            else "SWE-bench只批准正式Workspace Patch或当前公开测试Profile"
        ),
    )


async def _await_cancel[T](operation: Awaitable[T], cancel: CancelToken) -> T:
    return await cancel.run(operation)


def _turn_budget(plan: SweBenchPreRegistrationPlan) -> Budget:
    budget = plan.task_budget
    return Budget(
        max_steps=budget.max_steps,
        max_tokens=budget.max_tokens,
        timeout_seconds=budget.timeout_seconds,
        max_output_chars=budget.max_output_chars,
        max_tool_calls_per_step=budget.max_tool_calls_per_step,
    )


def _require_agent_action_gateway(
    gateway: RouterBackedAgentActionGateway | None,
    process_profile: ProductProcessProfile,
) -> RouterBackedAgentActionGateway:
    """模型调用前确认 Patch 与当前唯一 Profile 均已通过能力探测。"""

    if gateway is None:
        raise KernelError("swebench_action_unavailable", "SWE-bench Patch或公开测试Profile不可用")
    definitions = gateway.definitions()
    patch = workspace_patch_descriptor()
    profile = process_profile_descriptor(process_profile)
    patches = tuple(item for item in definitions if item.name == WORKSPACE_PATCH_TOOL)
    profiles = tuple(item for item in definitions if item.name.startswith("run_profile."))
    if (
        len(patches) != 1
        or patches[0].version != patch.version
        or len(profiles) != 1
        or profiles[0].name != process_tool_name(process_profile.profile_id)
        or profiles[0].version != profile.version
    ):
        raise KernelError("swebench_action_unavailable", "SWE-bench Patch或公开测试Profile不可用")
    return gateway


async def _drive_turn(
    runtime: AgentRuntime,
    owner: TaskPackPublicationOwner,
    plan: SweBenchPreRegistrationPlan,
    case: SweBenchPublicCase,
    workspace: Path,
    execution_fingerprint: str,
    process_profile: ProductProcessProfile,
    cancel: CancelToken,
    expected_terminal_turn: tuple[UUID, UUID] | None = None,
) -> tuple[UUID, Turn]:
    history = TaskPackHistoryReadControl.begin(cancel)
    thread = await owner.authenticated_single_thread(history)
    if expected_terminal_turn is not None and (
        thread is None or thread.thread_id != expected_terminal_turn[0]
    ):
        raise KernelError("swebench_case_evidence_mismatch", "SWE-bench冻结的Thread证明缺失或漂移")
    if thread is None:
        thread = await runtime.create_thread(str(workspace))
    elif thread.workspace != str(workspace):
        raise KernelError("swebench_run_projection_invalid", "SWE-bench Thread与Workspace不一致")

    request_id = (
        f"swebench:{plan.plan_sha256}:{execution_fingerprint}:{case.instance_id}:{_PROMPT_VERSION}"
    )
    turn = _request_turn(thread, request_id)
    if expected_terminal_turn is not None:
        if (
            turn is None
            or turn.turn_id != expected_terminal_turn[1]
            or turn.status not in TERMINAL_TURNS
        ):
            raise KernelError(
                "swebench_case_evidence_mismatch", "SWE-bench冻结的Turn证明缺失或漂移"
            )
        # 终态恢复只能读取原事实；不能因证据缺失创建第二次付费尝试。
        return thread.thread_id, turn
    if turn is None:
        budget = _turn_budget(plan)
        turn = await _await_cancel(
            runtime.run_turn(
                thread.thread_id,
                build_swebench_agent_prompt(case, budget),
                request_id=request_id,
                budget=budget,
            ),
            cancel,
        )

    while turn.status not in TERMINAL_TURNS:
        cancel.checkpoint()
        if turn.status is TurnStatus.WAITING_INPUT:
            # pass@1 不允许把人工回答或额外提示注入同一 Case。
            turn = await _await_cancel(runtime.cancel(thread.thread_id, turn.turn_id), cancel)
            continue
        if remaining_seconds(turn) <= 0:
            turn = await _await_cancel(runtime.resume_turn(thread.thread_id, turn.turn_id), cancel)
            continue
        if turn.status is TurnStatus.WAITING_APPROVAL:
            approval = _pending_approval(turn)
            if approval is None:
                turn = await _await_cancel(
                    runtime.resume_turn(thread.thread_id, turn.turn_id), cancel
                )
                continue
            turn = await _await_cancel(
                runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=_approval_decision(turn, approval, process_profile),
                ),
                cancel,
            )
            turn = await _await_cancel(runtime.resume_turn(thread.thread_id, turn.turn_id), cancel)
            continue
        if turn.status in {
            TurnStatus.ACCEPTED,
            TurnStatus.EXECUTING_TOOLS,
            TurnStatus.WAITING_ACTION,
        }:
            turn = await _await_cancel(runtime.resume_turn(thread.thread_id, turn.turn_id), cancel)
            continue
        raise KernelError("swebench_run_projection_invalid", "SWE-bench Turn停留在不可恢复状态")
    return thread.thread_id, turn


def _provider(config: OpenAIChatConfig, api_key: str) -> AbstractAsyncContextManager[ModelProvider]:
    return OpenAIChatProvider(config, api_key=api_key)


async def run_swebench_agent_case(
    *,
    plan: SweBenchPreRegistrationPlan,
    case: SweBenchPublicCase,
    run_root: Path,
    workspace: Path,
    git_executable: Path,
    provider_config: OpenAIChatConfig,
    execution_fingerprint: str,
    process_profile: ProductProcessProfile,
    api_key: str,
    cancel: CancelToken | None = None,
    observability: Observability | None = None,
    expected_terminal_turn: tuple[UUID, UUID] | None = None,
) -> tuple[UUID, Turn]:
    """执行或恢复一个 Case；Session、审批与 Patch 均走正式产品端口。"""

    if len(execution_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in execution_fingerprint
    ):
        raise KernelError("swebench_execution_config_invalid", "SWE-bench执行配置摘要无效")
    token = cancel or CancelToken()
    if expected_terminal_turn is not None and not (run_root / "session.sqlite").is_file():
        raise KernelError("swebench_case_evidence_mismatch", "SWE-bench冻结的Session证明缺失")
    with provider_publication_scope(provider_config.api_key_env, api_key) as scope:
        history = TaskPackHistoryReadControl.begin(token)
        async with open_task_pack_publication(run_root, scope, history_read=history) as owner:
            # Provider 打开前先认证已有完整历史，禁止用新凭据掩盖损坏状态。
            await owner.authenticated_single_thread(history)
            async with _provider(provider_config, api_key) as provider:
                async with CodingToolRuntime(
                    workspace,
                    artifacts=owner.artifacts,
                    git_executable=git_executable,
                    git_state_directory=run_root,
                    git_output_redaction=owner.scope,
                ) as tools:
                    async with open_default_product_action_runtime(
                        run_root,
                        workspace,
                        owner.artifacts,
                        _NoSecrets(),
                        build_product_action_config(process_profiles=(process_profile,)),
                        artifact_workspace_scope=tools.workspace_scope,
                        output_redaction=owner.scope,
                        root_owner=owner.root_owner,
                    ) as actions:
                        gateway = _require_agent_action_gateway(actions.gateway, process_profile)
                        context = build_product_agent_context(
                            tools.workspace_root,
                            max_output_tokens=provider_config.max_output_tokens,
                            require_pre_edit_profile=False,
                        )
                        async with AgentRuntime(
                            owner.sessions,
                            provider,
                            scoped_tools=tools,
                            trusted_actions=gateway,
                            artifacts=owner.artifacts,
                            public_output_protection=owner.scope,
                            observability=observability or NoOpObservability(),
                            async_context=context.context,
                            compaction=context.compaction,
                            summary_provider=provider,
                            model_tool_view=CodingWorkflowToolView(require_pre_edit_profile=False),
                        ) as runtime:
                            return await _drive_turn(
                                runtime,
                                owner,
                                plan,
                                case,
                                workspace,
                                execution_fingerprint,
                                process_profile,
                                token,
                                expected_terminal_turn,
                            )
