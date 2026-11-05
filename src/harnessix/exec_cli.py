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
from uuid import uuid4

from harnessix.agent_cli import ApprovalOutcome, ApprovalPolicy, ThinAgentCLI
from harnessix.product_config.preflight import run_product_preflight
from harnessix.product_ui.cli import (
    _action_config,
    _default_config,
    _default_state_directory,
    _preflight_request,
    _render_preflight,
    _server_command,
    _workspace,
)
from harnessix.product_ui.turn_budget import PRODUCT_TURN_BUDGET
from harnessix.protocol.contracts import (
    PublicApprovalRequestContent,
    PublicBudget,
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
    parser.add_argument("--workspace", default=".", help="工作区目录")
    parser.add_argument("--config", help="产品配置路径")
    parser.add_argument("--profile", help="模型Profile")
    parser.add_argument("--action-config", help="Action配置路径（启用容器命令等）")
    parser.add_argument("--state-directory", help="产品状态目录")
    parser.add_argument("--git-executable", help="git可执行文件")
    parser.add_argument(
        "--approval-mode", choices=("manual", "auto-edit", "auto"), default="manual"
    )
    defaults = PRODUCT_TURN_BUDGET
    parser.add_argument("--max-steps", type=int, default=defaults.max_steps)
    parser.add_argument("--max-tokens", type=int, default=defaults.max_tokens)
    parser.add_argument("--timeout-seconds", type=float, default=defaults.timeout_seconds)
    parser.add_argument("--max-output-chars", type=int, default=defaults.max_output_chars)
    parser.add_argument(
        "--max-tool-calls-per-step", type=int, default=defaults.max_tool_calls_per_step
    )
    parser.add_argument("--summary-file", help="把JSON摘要写入该文件（默认写到stdout）")
    parser.add_argument("--quiet", action="store_true", help="不输出过程文本")
    return parser


def _prompt(arguments: argparse.Namespace) -> str:
    if arguments.prompt is not None:
        return str(arguments.prompt)
    return Path(arguments.prompt_file).read_text(encoding="utf-8")


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


def _summary(
    cli: ThinAgentCLI,
    thread: ThreadView,
    *,
    mode: ApprovalMode,
    needs_input: bool,
    elapsed: float,
    changed: list[str] | None,
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
        "approval_mode": mode,
        "approvals": approvals,
        "final_answer": answers[-1] if answers else None,
        "changed_paths": changed,
        "commands": _commands(items),
        "elapsed_seconds": round(elapsed, 3),
    }


@dataclass(frozen=True)
class _Launch:
    command: tuple[str, ...]
    workspace: Path
    prompt: str
    budget: PublicBudget
    mode: ApprovalMode
    quiet: bool


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
        config=config,
        action_config=_action_config(arguments.action_config),
        profile=arguments.profile,
        workspace=workspace,
        runtime_state=state_root / "runtime",
        git_executable=arguments.git_executable,
    )
    budget = PublicBudget(
        max_steps=arguments.max_steps,
        max_tokens=arguments.max_tokens,
        timeout_seconds=arguments.timeout_seconds,
        max_output_chars=arguments.max_output_chars,
        max_tool_calls_per_step=arguments.max_tool_calls_per_step,
    )
    return _Launch(
        command=command,
        workspace=workspace,
        prompt=_prompt(arguments),
        budget=budget,
        mode=arguments.approval_mode,
        quiet=arguments.quiet,
    )


async def _run_turn(launch: _Launch) -> tuple[ThinAgentCLI, ThreadView, bool]:
    """运行单个Turn；非交互遇到人工输入时取消活动Turn并标记needs_input。"""

    console = HeadlessConsole(quiet=launch.quiet, interactive=sys.stdin.isatty())
    client = AgentClient(SubprocessAgentTransport(launch.command), client_name="harnessix-exec")
    try:
        await client.initialize()
        created = await client.create_thread(str(launch.workspace), request_id=f"exec-{uuid4()}")
        cli = ThinAgentCLI(
            client,
            console=console,
            actor=f"harnessix-exec:{launch.mode}",
            approval_policy=approval_policy(launch.mode),
        )
        try:
            thread = await cli.run_turn(
                created.thread_id, launch.prompt, request_id=f"exec-{uuid4()}", budget=launch.budget
            )
            return cli, thread, False
        except NeedsHumanInput:
            thread = await client.get_thread(created.thread_id)
            if thread.active_turn_id is not None:
                await client.cancel_turn(
                    created.thread_id, thread.active_turn_id, request_id=f"exec-{uuid4()}"
                )
                thread = await client.get_thread(created.thread_id)
            return cli, thread, True
    finally:
        await client.close()


def exec_main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
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
