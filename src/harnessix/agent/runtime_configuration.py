"""Agent Runtime构造参数与持久审批能力的集中失败关闭规则。"""

from __future__ import annotations

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemContent,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    TrustedActionApprovalRequestContent,
)


def validate_runtime_switches(
    max_parallel_tools: int,
    enable_questions: bool,
    tool_runtimes: tuple[object | None, object | None],
    context_runtimes: tuple[object | None, object | None],
    compaction_runtimes: tuple[object | None, object | None],
) -> None:
    """校验互斥入口和必须成对提供的Runtime配置。"""

    if type(max_parallel_tools) is not int or not 1 <= max_parallel_tools <= 16:
        raise KernelError("tool_concurrency_invalid", "并行工具上限必须在1到16之间")
    if type(enable_questions) is not bool:
        raise KernelError("question_runtime_invalid", "提问能力开关必须是布尔值")
    if all(item is not None for item in tool_runtimes):
        raise KernelError("tool_runtime_conflict", "旧工具入口与 Scoped 入口不能同时配置")
    if all(item is not None for item in context_runtimes):
        raise KernelError("context_runtime_conflict", "同步与异步 Context 入口不能同时配置")
    if (compaction_runtimes[0] is None) != (compaction_runtimes[1] is None):
        raise KernelError(
            "compaction_runtime_incomplete",
            "自动压缩配置与摘要Provider必须同时提供",
        )


def uses_approval_boundary(
    call_requires_read_approval: bool,
    *,
    trusted_action: bool,
    process: bool,
    patch: bool,
    patch_batch: bool,
) -> bool:
    """合并统一Action与兼容专用端口的审批入口判定。"""

    return any((trusted_action, process, patch, patch_batch, call_requires_read_approval))


def ensure_approval_runtime(
    content: ItemContent,
    *,
    patch_enabled: bool,
    batch_enabled: bool,
    process_enabled: bool,
    trusted_action_enabled: bool,
) -> None:
    """持久审批存在时要求原专用执行能力仍由同一产品组合提供。"""

    if isinstance(content, PatchBatchApprovalRequestContent) and not batch_enabled:
        raise KernelError("patch_batch_not_enabled", "持久整组审批缺少原专用端口")
    if isinstance(content, PatchApprovalRequestContent) and not patch_enabled:
        raise KernelError("patch_not_enabled", "持久单文件审批缺少原专用端口")
    if isinstance(content, ProcessApprovalRequestContent) and not process_enabled:
        raise KernelError("process_not_enabled", "持久Process审批缺少原专用端口")
    if isinstance(content, TrustedActionApprovalRequestContent) and not trusted_action_enabled:
        raise KernelError("trusted_action_not_enabled", "持久Trusted Action审批缺少原Gateway")
