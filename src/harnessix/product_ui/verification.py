"""一轮任务的完成证据：最后一次成功修改代码之后，是否运行过检查、结果如何。

`harnessix exec`的摘要与终端界面共用这一判断。模型的总结可能声称跑过测试而实际没有
（Beta Issue #9），这里只看这一轮里真正发生过的工具调用与结果。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harnessix.protocol.contracts import PublicItem, PublicToolCallContent, PublicToolResultContent

PATCH_TOOLS = ("apply_patch", "apply_patch_batch")
CHECK_TOOL_PREFIXES = ("exec_command.", "run_profile.")


def verification_summary(items: Sequence[PublicItem]) -> dict[str, Any]:
    """完成证据：最后一次成功改代码之后是否运行过检查，以及最后一次检查的结果。"""

    results = {
        item.content.call_id: item.content
        for item in items
        if isinstance(item.content, PublicToolResultContent)
    }
    edited = False
    last_check: tuple[str | None, str | None] | None = None
    for item in items:
        call = item.content
        if not isinstance(call, PublicToolCallContent):
            continue
        result = results.get(call.call_id)
        outcome = None if result is None else result.outcome
        if call.tool in PATCH_TOOLS and outcome == "succeeded":
            edited, last_check = True, None  # 之后的检查才算数
        elif call.tool.startswith(CHECK_TOOL_PREFIXES) and edited:
            command = call.arguments.get("cmd")
            last_check = (command if isinstance(command, str) else call.tool, outcome)
    if not edited:
        return {"status": "no_changes"}
    if last_check is None:
        return {"status": "unverified"}
    command, check_outcome = last_check
    passed = check_outcome == "succeeded"
    status = "passed_after_last_edit" if passed else "failed_after_last_edit"
    return {"status": status, "last_check": command}


def unverified_notice(items: Sequence[PublicItem]) -> str | None:
    """一轮已完成的任务该附带的提示：最后一次修改之后没有检查，或最后一次检查没有通过；否则None。"""

    summary = verification_summary(items)
    if summary["status"] == "unverified":
        return (
            "本轮最后一次修改之后没有运行过测试或其他检查；"
            "回复里提到的检查结果不是在这些修改之后得到的，请自行确认"
        )
    if summary["status"] == "failed_after_last_edit":
        return (
            f"本轮最后一次修改之后，最后一次检查没有通过（{summary['last_check']}）；"
            "请先确认再使用这些修改"
        )
    return None
