"""一轮任务的完成证据：最后一次成功修改代码之后，是否运行过检查、结果如何。

`harnessix exec`的摘要与终端界面共用这一判断。模型的总结可能声称跑过测试而实际没有
（Beta Issue #9），这里只看这一轮里真正发生过的工具调用与结果。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from harnessix.protocol.contracts import PublicItem, PublicToolCallContent, PublicToolResultContent

PATCH_TOOLS = ("apply_patch", "apply_patch_batch")
# 沙箱内命令、沙箱外命令（沙箱不可用时逐条审批，Windows与没有Seatbelt的macOS）与检查配置。
CHECK_TOOL_PREFIXES = ("exec_command.", "host_command.", "run_profile.")
# Windows的PowerShell命令在最后一条语句成功、而最后一个原生程序失败时写到stderr末尾的一行
# （product_config.native_shells.NATIVE_EXIT_NOTE，Beta Issue #27）。
_NATIVE_EXIT = re.compile(r"harnessix: the last native program exited with (-?\d+)")


def verification_summary(items: Sequence[PublicItem]) -> dict[str, Any]:
    """完成证据：最后一次成功改代码之后是否运行过检查，以及最后一次检查的结果。"""

    results = {
        item.content.call_id: item.content
        for item in items
        if isinstance(item.content, PublicToolResultContent)
    }
    edited = False
    last_check: tuple[str | None, str | None, int | None] | None = None
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
            native = _native_exit_code(result)
            last_check = (command if isinstance(command, str) else call.tool, outcome, native)
    if not edited:
        return {"status": "no_changes"}
    if last_check is None:
        return {"status": "unverified"}
    command, check_outcome, native = last_check
    # 命令本身成功、但其中最后一个原生程序失败（测试之后又打印了一行）不算通过。
    passed = check_outcome == "succeeded" and not native
    status = "passed_after_last_edit" if passed else "failed_after_last_edit"
    summary: dict[str, Any] = {"status": status, "last_check": command}
    if native:
        summary["last_native_returncode"] = native
    return summary


def _native_exit_code(result: PublicToolResultContent | None) -> int | None:
    """命令stderr尾部注明的最后一个原生程序的非零退出码；没有时为None。"""

    output = None if result is None else result.output
    preview = output.get("diagnostic_preview") if isinstance(output, dict) else None
    stderr = preview.get("stderr") if isinstance(preview, dict) else None
    text = stderr.get("text") if isinstance(stderr, dict) else None
    found = _NATIVE_EXIT.findall(text) if isinstance(text, str) else []
    return int(found[-1]) if found else None


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
