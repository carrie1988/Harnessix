"""一次性无头执行：按产品配置启动App Server，运行单个Turn，输出机器可读的结果摘要。

审批模式只决定由谁回应审批请求；每个决定仍经App Server写入可信动作审计。沙箱、网络与凭据边界
由产品与Action配置决定，审批模式不能放宽它们。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from harnessix.agent_cli import ApprovalOutcome, ApprovalPolicy, ThinAgentCLI
from harnessix.product_config.preflight import run_product_preflight
from harnessix.product_ui.cli import (
    _action_config,
    _chain_pricing,
    _default_config,
    _default_state_directory,
    _preflight_request,
    _render_preflight,
    _server_command,
    _workspace,
)
from harnessix.product_ui.git_detection import detected_git_executable
from harnessix.product_ui.preflight_text import sandbox_status_line
from harnessix.product_ui.turn_budget import turn_budget_for
from harnessix.product_ui.workspace_trust import confirm_workspace_trust
from harnessix.protocol.contracts import (
    PublicApprovalRequestContent,
    PublicBudget,
    PublicEvent,
    PublicItem,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolResultContent,
    ThreadView,
)
from harnessix.sdk import AgentClient, AgentSDKError, SubprocessAgentTransport

ApprovalMode = Literal["manual", "auto-edit", "auto"]
# 只在容器沙箱内执行的进程类工具；其他工具始终需要人工确认。
_SANDBOXED_PROCESS_TOOL_PREFIXES = ("exec_command.", "run_profile.")

EXIT_COMPLETED = 0
EXIT_TURN_FAILED = 1
EXIT_NEEDS_INPUT = 2
EXIT_NOT_READY = 3
EXIT_INTERNAL = 4


def approval_policy(mode: ApprovalMode) -> ApprovalPolicy:
    """manual全部交给人；auto-edit自动批准工作区补丁；auto再自动批准沙箱内进程调用。"""

    def decide(content: PublicApprovalRequestContent, tool: str | None) -> ApprovalOutcome | None:
        if mode == "manual":
            return None
        if content.approval_type in ("patch", "patch_batch"):
            return "approved"
        sandboxed = tool is not None and tool.startswith(_SANDBOXED_PROCESS_TOOL_PREFIXES)
        if mode == "auto" and content.approval_type == "process" and sandboxed:
            return "approved"
        return None

    return decide


class NeedsHumanInput(Exception):
    """非交互运行遇到只能由人回答的审批或问题。"""


class HeadlessConsole:
    """过程文本写到stderr（可静默）；非交互时把人工提示转为NeedsHumanInput。"""

    def __init__(self, *, quiet: bool, interactive: bool) -> None:
        self._quiet = quiet
        self._interactive = interactive

    def write(self, text: str, *, end: str = "\n") -> None:
        if not self._quiet:
            print(text, end=end, file=sys.stderr, flush=True)

    async def prompt(self, text: str) -> str:
        if not self._interactive:
            raise NeedsHumanInput(text)
        return await asyncio.to_thread(input, text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix exec", description="无头运行一个Coding Agent任务并输出JSON摘要"
    )
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--prompt", help="任务描述")
    task.add_argument("--prompt-file", help="从UTF-8文件读取任务描述")
    task.add_argument(
        "--resume-thread",
        type=UUID,
        help="宿主或进程中断后，用同一--state-directory恢复该Thread并跟随其Turn到终态",
    )
    parser.add_argument("--workspace", default=".", help="工作区目录")
    parser.add_argument(
        "--trust-workspace",
        action="store_true",
        help="本次运行信任工作区（不记录）；用于CI等无法交互确认的场景",
    )
    parser.add_argument("--config", help="产品配置路径")
    parser.add_argument("--profile", help="模型Profile")
    parser.add_argument("--action-config", help="Action配置路径（启用容器命令等）")
    parser.add_argument("--state-directory", help="产品状态目录")
    parser.add_argument("--git-executable", help="git可执行文件")
    parser.add_argument(
        "--approval-mode", choices=("manual", "auto-edit", "auto"), default="manual"
    )
    # 未指定的预算项取产品默认；活动Profile链全为本地免费端点时累计token上限更高。
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--max-output-chars", type=int)
    parser.add_argument("--max-tool-calls-per-step", type=int)
    parser.add_argument("--summary-file", help="把JSON摘要写入该文件（默认写到stdout）")
    parser.add_argument("--events-file", help="按发生顺序把公开事件逐行写成JSONL")
    parser.add_argument("--quiet", action="store_true", help="不输出过程文本")
    return parser


def _prompt(arguments: argparse.Namespace) -> str | None:
    if arguments.prompt is not None:
        return str(arguments.prompt)
    if arguments.prompt_file is not None:
        return Path(arguments.prompt_file).read_text(encoding="utf-8")
    return None


def _changed_paths(workspace: Path, git_executable: str | None) -> list[str] | None:
    """工作区相对HEAD的变更路径；不是Git仓库时为None。不获取可选锁，不改写index。"""

    command = [git_executable or "git", "-C", str(workspace), "status", "--porcelain=v1", "-z"]
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        completed = subprocess.run(
            command, capture_output=True, check=False, env=environment, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    entries = completed.stdout.decode("utf-8", errors="replace").split("\0")
    return sorted({entry[3:] for entry in entries if len(entry) > 3})


def _commands(items: Sequence[PublicItem]) -> list[dict[str, Any]]:
    """按调用顺序列出沙箱命令及其结果，供外层编排复用候选自己的检查命令。"""

    results = {
        item.content.call_id: item.content
        for item in items
        if isinstance(item.content, PublicToolResultContent)
    }
    commands = []
    for item in items:
        call = item.content
        if not isinstance(call, PublicToolCallContent) or not call.tool.startswith("exec_command."):
            continue
        result = results.get(call.call_id)
        output = result.output if result is not None else None
        commands.append(
            {
                "tool": call.tool,
                "cmd": call.arguments.get("cmd"),
                "outcome": None if result is None else result.outcome,
                "returncode": output.get("returncode") if isinstance(output, dict) else None,
            }
        )
    return commands


_PATCH_TOOLS = ("apply_patch", "apply_patch_batch")
_CHECK_TOOL_PREFIXES = ("exec_command.", "run_profile.")


def _verification(items: Sequence[PublicItem]) -> dict[str, Any]:
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
        if call.tool in _PATCH_TOOLS and outcome == "succeeded":
            edited, last_check = True, None  # 之后的检查才算数
        elif call.tool.startswith(_CHECK_TOOL_PREFIXES) and edited:
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


def _summary(
    cli: ThinAgentCLI,
    thread: ThreadView,
    *,
    mode: ApprovalMode,
    needs_input: bool,
    elapsed: float,
    changed: list[str] | None,
    pricing: Literal["free", "unknown"] = "unknown",
) -> dict[str, Any]:
    turn = thread.latest_turn
    items = cli.recorded_items()
    answers = [
        item.content.text
        for item in items
        if isinstance(item.content, PublicTextContent) and item.content.kind == "assistant_message"
    ]
    approvals = [
        {
            "type": item.content.approval_type,
            "outcome": None if item.content.decision is None else item.content.decision.outcome,
            "actor": None if item.content.decision is None else item.content.decision.actor,
        }
        for item in items
        if isinstance(item.content, PublicApprovalRequestContent)
    ]
    return {
        "spec_version": "harnessix.exec-summary/v1",
        "thread_id": str(thread.thread_id),
        "turn_id": None if turn is None else str(turn.turn_id),
        "status": "needs_input" if needs_input else (None if turn is None else turn.status),
        "error": None if turn is None or turn.error is None else turn.error.model_dump(),
        "model_steps": None if turn is None else turn.model_steps,
        "usage": None if turn is None else turn.usage.model_dump(),
        # 本地自托管端点计0元；其他端点不公开价格，费用未知。
        "cost": {"pricing": pricing, "amount_cny": 0 if pricing == "free" else None},
        "approval_mode": mode,
        "approvals": approvals,
        "final_answer": answers[-1] if answers else None,
        "changed_paths": changed,
        "commands": _commands(items),
        "verification": _verification(items),
        "elapsed_seconds": round(elapsed, 3),
    }


@dataclass(frozen=True)
class _Launch:
    command: tuple[str, ...]
    workspace: Path
    prompt: str | None
    budget: PublicBudget
    mode: ApprovalMode
    quiet: bool
    pricing: Literal["free", "unknown"] = "unknown"
    events: Path | None = None
    resume_thread: UUID | None = None
    sandbox: str | None = None


def _or[T](value: T | None, default: T) -> T:
    return default if value is None else value


def _prepare(arguments: argparse.Namespace) -> _Launch | None:
    """同步完成启动预检与参数解析；未就绪时打印预检报告并返回None。"""

    preflight = run_product_preflight(
        _preflight_request(arguments, mode="startup", require_tui=False)
    )
    if not preflight.ready:
        print(_render_preflight(preflight), file=sys.stderr)
        return None
    workspace = _workspace(arguments.workspace)
    config = Path(arguments.config).absolute() if arguments.config else _default_config()
    state_root = (
        Path(arguments.state_directory).absolute()
        if arguments.state_directory
        else _default_state_directory(workspace)
    )
    command = _server_command(
        unattended=True,
        config=config,
        action_config=_action_config(arguments.action_config),
        profile=arguments.profile,
        workspace=workspace,
        runtime_state=state_root / "runtime",
        git_executable=arguments.git_executable,
    )
    pricing = _chain_pricing(config, arguments.profile)
    defaults = turn_budget_for(pricing)
    budget = PublicBudget(
        max_steps=_or(arguments.max_steps, defaults.max_steps),
        max_tokens=_or(arguments.max_tokens, defaults.max_tokens),
        timeout_seconds=_or(arguments.timeout_seconds, defaults.timeout_seconds),
        max_output_chars=_or(arguments.max_output_chars, defaults.max_output_chars),
        max_tool_calls_per_step=_or(
            arguments.max_tool_calls_per_step, defaults.max_tool_calls_per_step
        ),
    )
    return _Launch(
        command=command,
        workspace=workspace,
        prompt=_prompt(arguments),
        budget=budget,
        mode=arguments.approval_mode,
        quiet=arguments.quiet,
        pricing=pricing,
        events=Path(arguments.events_file).absolute() if arguments.events_file else None,
        resume_thread=arguments.resume_thread,
        sandbox=sandbox_status_line(preflight),
    )


async def _run_turn(launch: _Launch) -> tuple[ThinAgentCLI, ThreadView, bool]:
    """运行单个Turn；非交互遇到人工输入时取消活动Turn并标记needs_input。"""

    console = HeadlessConsole(quiet=launch.quiet, interactive=sys.stdin.isatty())
    client = AgentClient(SubprocessAgentTransport(launch.command), client_name="harnessix-exec")
    events = None if launch.events is None else launch.events.open("w", encoding="utf-8")

    def emit(event: PublicEvent) -> None:
        assert events is not None
        events.write(event.model_dump_json(by_alias=True) + "\n")
        events.flush()

    if launch.sandbox is not None:
        console.write(launch.sandbox)
    try:
        await client.initialize()
        cli = ThinAgentCLI(
            client,
            console=console,
            actor=f"harnessix-exec:{launch.mode}",
            approval_policy=approval_policy(launch.mode),
            on_event=None if events is None else emit,
        )
        if launch.resume_thread is not None:
            thread_id = launch.resume_thread
        else:
            created = await client.create_thread(
                str(launch.workspace), request_id=f"exec-{uuid4()}"
            )
            thread_id = created.thread_id
        try:
            if launch.prompt is None:
                # 恢复：App Server启动时先对账未决效果，再跟随原Turn到终态，不重放未知效果。
                return cli, await cli.follow(thread_id), False
            thread = await cli.run_turn(
                thread_id, launch.prompt, request_id=f"exec-{uuid4()}", budget=launch.budget
            )
            return cli, thread, False
        except NeedsHumanInput:
            thread = await client.get_thread(thread_id)
            if thread.active_turn_id is not None:
                await client.cancel_turn(
                    thread_id, thread.active_turn_id, request_id=f"exec-{uuid4()}"
                )
                thread = await client.get_thread(thread_id)
            return cli, thread, True
    finally:
        await client.close()
        if events is not None:
            events.close()


def exec_main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if not confirm_workspace_trust(
        Path(arguments.workspace).absolute(), assume_trusted=arguments.trust_workspace
    ):
        raise SystemExit(EXIT_NOT_READY)
    arguments.git_executable = detected_git_executable(
        arguments.git_executable, Path(arguments.workspace).absolute()
    )
    summary: dict[str, Any] | None = None
    try:
        launch = _prepare(arguments)
        if launch is None:
            raise SystemExit(EXIT_NOT_READY)
        started = time.monotonic()
        cli, thread, needs_input = asyncio.run(_run_turn(launch))
        summary = _summary(
            cli,
            thread,
            mode=launch.mode,
            needs_input=needs_input,
            elapsed=time.monotonic() - started,
            changed=_changed_paths(launch.workspace, arguments.git_executable),
            pricing=launch.pricing,
        )
        completed = thread.latest_turn is not None and thread.latest_turn.status == "completed"
        code = (
            EXIT_NEEDS_INPUT if needs_input else (EXIT_COMPLETED if completed else EXIT_TURN_FAILED)
        )
    except AgentSDKError as error:
        code, summary = EXIT_INTERNAL, {"status": "error", "error": {"code": error.code}}
    if summary is not None:
        text = json.dumps(summary, ensure_ascii=False, indent=2)
        if arguments.summary_file:
            Path(arguments.summary_file).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
    raise SystemExit(code)
