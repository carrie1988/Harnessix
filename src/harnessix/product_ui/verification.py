"""一轮任务的完成证据：最后一次成功修改代码之后，是否运行过检查、结果如何。

`harnessix exec`的摘要与终端界面共用这一判断。模型的总结可能声称跑过测试而实际没有
（Beta Issue #9），这里只看这一轮里真正发生过的工具调用与结果。

一条检查失败后，只有同一条命令重新运行并通过才算解决；之后其他命令的成功（如清理临时文件）
不覆盖它（Beta Issue #36）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from harnessix.protocol.contracts import PublicItem, PublicToolCallContent, PublicToolResultContent

PATCH_TOOLS = ("apply_patch", "apply_patch_batch")
# 沙箱内命令、沙箱外命令（沙箱不可用时逐条审批，Windows与没有Seatbelt的macOS）与检查配置。
CHECK_TOOL_PREFIXES = ("exec_command.", "host_command.", "run_profile.")
# Windows的PowerShell命令在最后一条语句成功、而最后一个原生程序失败时写到stderr末尾的一行
# （product_config.native_shells.NATIVE_EXIT_NOTE，Beta Issue #27）。
_NATIVE_EXIT = re.compile(r"harnessix: the last native program exited with (-?\d+)")


@dataclass(frozen=True, slots=True)
class _Check:
    """一次检查：命令（没有cmd时是工具名）、是否通过、最后一个原生程序的非零退出码。"""

    command: str
    passed: bool
    native: int | None


def verification_summary(items: Sequence[PublicItem]) -> dict[str, Any]:
    """完成证据：最后一次成功改代码之后是否运行过检查，以及检查有没有全部通过。

    `last_check`是最后运行的检查；`failed_check`是仍未通过的检查中最近运行的一条（失败时才有）。
    """

    results = {
        item.content.call_id: item.content
        for item in items
        if isinstance(item.content, PublicToolResultContent)
    }
    edited = False
    # 每条检查命令最近一次的结果，按最近运行的先后排列：同一命令再运行即取代原结果。
    checks: dict[str, _Check] = {}
    for item in items:
        call = item.content
        if not isinstance(call, PublicToolCallContent):
            continue
        result = results.get(call.call_id)
        if call.tool in PATCH_TOOLS and result is not None and result.outcome == "succeeded":
            edited = True
            checks.clear()  # 之后的检查才算数
        elif call.tool.startswith(CHECK_TOOL_PREFIXES) and edited:
            check = _check(call, result)
            checks.pop(check.command, None)
            checks[check.command] = check
    if not edited:
        return {"status": "no_changes"}
    if not checks:
        return {"status": "unverified"}
    summary: dict[str, Any] = {
        "status": "passed_after_last_edit",
        "last_check": list(checks)[-1],
    }
    failed = [check for check in checks.values() if not check.passed]
    if failed:
        summary.update(status="failed_after_last_edit", failed_check=failed[-1].command)
        if failed[-1].native:
            summary["last_native_returncode"] = failed[-1].native
    return summary


def _check(call: PublicToolCallContent, result: PublicToolResultContent | None) -> _Check:
    command = call.arguments.get("cmd")
    native = _native_exit_code(result)
    # 命令本身成功、但其中最后一个原生程序失败（测试之后又打印了一行）不算通过。
    passed = result is not None and result.outcome == "succeeded" and not native
    return _Check(command if isinstance(command, str) else call.tool, passed, native)


def _native_exit_code(result: PublicToolResultContent | None) -> int | None:
    """命令stderr尾部注明的最后一个原生程序的非零退出码；没有时为None。"""

    output = None if result is None else result.output
    preview = output.get("diagnostic_preview") if isinstance(output, dict) else None
    stderr = preview.get("stderr") if isinstance(preview, dict) else None
    text = stderr.get("text") if isinstance(stderr, dict) else None
    found = _NATIVE_EXIT.findall(text) if isinstance(text, str) else []
    return int(found[-1]) if found else None


def unverified_notice(items: Sequence[PublicItem]) -> str | None:
    """一轮已完成的任务该附带的提示：最后一次修改之后没有检查，或有检查没有通过；否则None。"""

    summary = verification_summary(items)
    if summary["status"] == "unverified":
        return (
            "本轮最后一次修改之后没有运行过测试或其他检查；"
            "回复里提到的检查结果不是在这些修改之后得到的，请自行确认"
        )
    if summary["status"] == "failed_after_last_edit":
        return (
            f"本轮最后一次修改之后有检查没有通过（{summary['failed_check']}），"
            "之后没有重新运行这条检查并通过；请先确认再使用这些修改"
        )
    return None
