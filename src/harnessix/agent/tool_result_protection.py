"""工具结果公开前的保护检查做不完时，只让那一次只读调用失败（ADR 0174，Beta Issue #37）。

检查超出资源上限或期限不等于发现Secret：结果正文不公开，换成一条说明原因、可由模型调整请求的失败结果，
本轮与同批其他调用照常继续。发现Secret、保护能力缺失等其他拒绝，以及带副作用的结果，照旧失败关闭。
"""

from __future__ import annotations

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.agent.publication import (
    PROTECTION_LIMIT_FAILURES,
    PublicOutputProtection,
    protect_json,
)
from harnessix.domain.models import EffectClass

PROTECTION_LIMIT_FEEDBACK = (
    "The result of this call was too large to check for protected values before it could be "
    "shown, so it was not shown and nothing else was affected. Retry with a narrower request, "
    "for example a more specific path or pattern, or fewer results."
)


async def protect_tool_result(
    protection: PublicOutputProtection | None,
    call: ToolCallContent,
    result: ToolResultContent,
) -> ToolResultContent:
    """返回可提交的结果：通过检查时原样返回，只读调用检查做不完时换成这一次调用的失败结果。"""
    try:
        await protect_json(protection, result.model_dump(mode="json"), CancelToken())
    except KernelError as error:
        if error.code not in PROTECTION_LIMIT_FAILURES or not _without_effects(call, result):
            raise
        return ToolResultContent(
            call_id=call.call_id,
            outcome="failed",
            error=AgentFailure(code=error.code, message=PROTECTION_LIMIT_FEEDBACK),
        )
    return result


def _without_effects(call: ToolCallContent, result: ToolResultContent) -> bool:
    effects = (result.patch, result.patch_batch, result.process, result.trusted_action)
    return (
        call.effect_class is EffectClass.READ_ONLY
        and result.diff_artifact is None
        and all(effect is None for effect in effects)
    )
