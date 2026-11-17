"""命令路径基准：一条简单Shell命令从工具调用到结果，宿主自身在各阶段花了多少时间。

职责：以评测同款装配（外部边界命令Profile、协议客户端自动审批）跑一个只发简单只读命令的脚本化
Turn，把每条命令的往返时间按公开事件、Action审计事件和Process Lease三处的时间戳切成阶段。命令自身
的运行时间用同一程序、同一环境直接启动子进程测得，往返时间减去它就是宿主开销。
非职责：不访问网络、不评价模型、不进入默认测试集；不给产品代码加探针，所有时间点都来自产品本来
就记录的事实。

用法（在仓库根目录）：
    PYTHONPATH=src:. python scripts/command_path_bench.py --commands 40 --json out.json
    PYTHONPATH=src:. python scripts/command_path_bench.py --commands 40 --profile
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID

from harnessix.models.contracts import ProviderEvent, ToolCallCompleted
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.protocol.contracts import PublicEvent
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from scripts.host_step_overhead_bench import (
    StepTiming,
    failed_tool_results,
    model_step,
    narration,
    profiled,
    run_scripted_turn,
    step_timings,
    tool_call,
)

COMMAND_TOOL = "exec_command.testbed"
# 往返内的阶段，按发生顺序；相加即工具调用项到结果项的往返时间。
ROUND_TRIP_PHASES = (
    "request_approval",
    "approval_round_trip",
    "resume",
    "process_start",
    "process_run",
    "capture",
    "publication",
)
# 结果之后、下一次模型调用之前的两段，不计入往返。
FOLLOWING_PHASES = ("result_commit", "preparing_context")
_BARE_RUNS = 7
_ROUTE_TERMINAL_STATES = frozenset({"succeeded", "failed", "unknown"})


@dataclass(frozen=True)
class CommandTiming:
    """一条命令各阶段的秒数。

    request_approval：工具调用项到审批请求项（分类、规划、落盘计划）。
    approval_round_trip：审批请求项到其完成项（客户端取到请求并答复，服务端记录决定）。
    resume：审批完成到审计记为running（恢复Turn，重新核对计划与审批）。
    process_start：审计running到Owner报告的进程启动时刻（Lease、运行目录、启动Owner）。
    process_run：Owner报告的启动到结束；含Owner发现退出的延迟，所以不等于命令自身的时间。
    capture：进程结束到审计终态（宿主发现终态回执、读取并核对输出）。
    publication：审计终态到结果项（发布输出Artifact、提交结果事件）。
    result_commit：结果项到下一步的preparing_context。
    preparing_context：下一步的preparing_context到calling_model。
    bare_command：同一命令不经宿主直接启动的中位耗时。
    """

    step: int
    command: str
    request_approval: float
    approval_round_trip: float
    resume: float
    process_start: float
    process_run: float
    capture: float
    publication: float
    result_commit: float
    preparing_context: float
    bare_command: float

    @property
    def round_trip(self) -> float:
        return sum(getattr(self, phase) for phase in ROUND_TRIP_PHASES)

    @property
    def host_overhead(self) -> float:
        return self.round_trip - self.bare_command


@dataclass(frozen=True)
class CommandPathReport:
    commands: tuple[CommandTiming, ...]
    steps: tuple[StepTiming, ...]
    wall_seconds: float
    reason: str
    failed_tool_results: int

    @property
    def valid(self) -> bool:
        return self.reason == "completed" and self.failed_tool_results == 0


def trivial_command(index: int) -> str:
    """评测里最常见的几类只读命令；输出在0.1–2 KB之间，与真实的浏览命令相当。"""
    path = f"inventory/warehouse_{index % 24:02d}.py"
    first = 1 + 11 * (index % 40)
    variants = (
        "ls inventory",
        f"sed -n '{first},{first + 30}p' {path}",
        f"grep -n 'reconcile_batch_{index % 24}_{index % 10}' {path}",
        f"head -n 40 {path}",
        f"wc -l {path}",
    )
    return variants[index % len(variants)]


def build_command_script(commands: int) -> tuple[list[list[ProviderEvent]], list[str]]:
    """一步补丁、commands步简单命令、一步纯文本收尾。

    补丁在最前：评测配置开着完成守卫，任务里没有任何改动时纯文本收尾会被要求继续。
    """
    script: list[list[ProviderEvent]] = []
    tools: list[str] = []
    for step in range(1, commands + 2):
        if step == 1:
            tool, arguments = tool_call("patch", step)
        else:
            tool, arguments = COMMAND_TOOL, {"cmd": trivial_command(step)}
        tools.append(tool)
        text = f"Step {step}: checking the next piece of evidence before deciding what to change."
        call = ToolCallCompleted(call_id=f"bench-call-{step}", tool=tool, arguments=arguments)
        script.append(model_step(step, [*narration(step, text), call]))
    summary = "The marker is updated and the surrounding modules were inspected."
    script.append(model_step(commands + 2, narration(commands + 2, summary)))
    return script, tools


def _moment(event: PublicEvent) -> float:
    return event.occurred_at.timestamp()


@dataclass
class _CommandMarks:
    """一条命令在公开事件里的时间点；缺任何一个都说明这一步没有按预期走完。"""

    command: str
    call: float
    approval_requested: float | None = None
    approval_answered: float | None = None
    process_id: UUID | None = None
    result: float | None = None
    next_preparing: float | None = None
    next_calling: float | None = None


def _command_marks(events: Sequence[PublicEvent]) -> list[_CommandMarks]:
    marks: list[_CommandMarks] = []
    current: _CommandMarks | None = None
    for event in events:
        data = event.data
        if data.type == "turn_state_changed":
            if current is None or current.result is None:
                continue
            if data.status == "preparing_context" and current.next_preparing is None:
                current.next_preparing = _moment(event)
            elif data.status == "calling_model" and current.next_calling is None:
                current.next_calling = _moment(event)
            continue
        if data.type not in {"item_started", "item_finished"}:
            continue
        content = data.item.content
        if content.kind == "tool_call" and data.type == "item_started":
            current = None
            if content.tool == COMMAND_TOOL:
                current = _CommandMarks(str(content.arguments["cmd"]), _moment(event))
                marks.append(current)
        elif current is None:
            continue
        elif content.kind == "approval_request":
            if data.type == "item_started":
                current.approval_requested = _moment(event)
            else:
                current.approval_answered = _moment(event)
        elif content.kind == "tool_result" and data.type == "item_finished":
            current.result = _moment(event)
            output = content.output
            if isinstance(output, Mapping) and isinstance(output.get("process_id"), str):
                current.process_id = UUID(str(output["process_id"]))
    return marks


def _audit_moments(store: SQLiteActionAuditStore, plan_id: UUID) -> tuple[float, float]:
    """审计事件里running与执行终态的时刻。"""
    running = terminal = None
    for event in store.events(plan_id):
        if event.to_state == "running":
            running = event.occurred_at.timestamp()
        elif event.to_state in _ROUTE_TERMINAL_STATES:
            terminal = event.occurred_at.timestamp()
    if running is None or terminal is None:
        raise ValueError(f"命令{plan_id}的审计事件不完整")
    return running, terminal


def _timestamp(value: datetime | None) -> float:
    if value is None:
        raise ValueError("Process Lease缺少启动或结束时间")
    return value.timestamp()


def command_timings(
    events: Sequence[PublicEvent], state: Path, bare_seconds: Mapping[str, float]
) -> tuple[CommandTiming, ...]:
    """把公开事件、审计事件与Lease的时间点拼成每条命令的阶段；三者来自同一台机器的墙钟。"""
    timings: list[CommandTiming] = []
    audit = SQLiteActionAuditStore(state / "action-audit.db", read_only=True)
    leases = SQLiteProcessLeaseStore(state / "process-owner" / "process-leases.db", read_only=True)
    with audit, leases:
        for step, marks in enumerate(_command_marks(events), start=1):
            if (
                marks.approval_requested is None
                or marks.approval_answered is None
                or marks.process_id is None
                or marks.result is None
                or marks.next_preparing is None
                or marks.next_calling is None
            ):
                raise ValueError(f"第{step}条命令的公开事件不完整")
            running, terminal = _audit_moments(audit, marks.process_id)
            lease = leases.load(marks.process_id)
            started, finished = _timestamp(lease.started_at), _timestamp(lease.finished_at)
            timings.append(
                CommandTiming(
                    step=step,
                    command=marks.command,
                    request_approval=marks.approval_requested - marks.call,
                    approval_round_trip=marks.approval_answered - marks.approval_requested,
                    resume=running - marks.approval_answered,
                    process_start=started - running,
                    process_run=finished - started,
                    capture=terminal - finished,
                    publication=marks.result - terminal,
                    result_commit=marks.next_preparing - marks.result,
                    preparing_context=marks.next_calling - marks.next_preparing,
                    bare_command=bare_seconds[marks.command],
                )
            )
    return tuple(timings)


def measure_bare_commands(
    commands: Sequence[str], *, workspace: Path, environment: Mapping[str, str]
) -> dict[str, float]:
    """与Profile相同的程序和参数直接启动子进程，取每条命令_BARE_RUNS次的中位数。"""
    bare: dict[str, float] = {}
    for command in dict.fromkeys(commands):
        samples: list[float] = []
        for _ in range(_BARE_RUNS):
            started = perf_counter()
            subprocess.run(  # noqa: S603 - 命令来自本脚本的固定列表
                ("/bin/bash", "-c", command),
                cwd=workspace,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
            )
            samples.append(perf_counter() - started)
        bare[command] = statistics.median(samples)
    return bare


async def run_command_bench(root: Path, *, commands: int = 40) -> CommandPathReport:
    """在root下跑完一个只发简单命令的Turn；root须为已存在的空目录，且不在任何工作区内。"""
    script, tools = build_command_script(commands)
    turn = await run_scripted_turn(root, script)
    issued = [trivial_command(step) for step in range(2, commands + 2)]
    environment = {"HOME": str(root), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    bare = measure_bare_commands(issued, workspace=root / "workspace", environment=environment)
    steps = step_timings(turn.events, tools, [0.0] * len(tools), turn.cpu_marks)
    return CommandPathReport(
        commands=command_timings(turn.events, turn.state, bare),
        steps=tuple(item for item in steps if item.tool == COMMAND_TOOL),
        wall_seconds=turn.wall_seconds,
        reason=turn.reason,
        failed_tool_results=failed_tool_results(turn.events),
    )


def quartiles(values: Sequence[float]) -> tuple[float, float, float]:
    """p25、中位数、p75；不足两个样本时三者相同。"""
    if len(values) < 2:
        return (values[0], values[0], values[0])
    low, median, high = statistics.quantiles(values, n=4, method="inclusive")
    return low, median, high


def summary(report: CommandPathReport) -> dict[str, tuple[float, float, float]]:
    """各阶段、往返、宿主开销与整步耗时的p25/中位数/p75（秒）。"""
    rows: dict[str, tuple[float, float, float]] = {}
    for name in (*ROUND_TRIP_PHASES, *FOLLOWING_PHASES, "bare_command"):
        rows[name] = quartiles([getattr(item, name) for item in report.commands])
    rows["round_trip"] = quartiles([item.round_trip for item in report.commands])
    rows["host_overhead"] = quartiles([item.host_overhead for item in report.commands])
    rows["step_total"] = quartiles([item.total for item in report.steps])
    rows["step_cpu"] = quartiles([item.cpu for item in report.steps])
    return rows


def format_report(report: CommandPathReport) -> str:
    lines = [f"{'phase':>20} {'p25':>9} {'median':>9} {'p75':>9}"]
    for name, (low, median, high) in summary(report).items():
        cells = " ".join(f"{value * 1000:>7.1f}ms" for value in (low, median, high))
        lines.append(f"{name:>20} {cells}")
    size = 10
    for start in range(0, len(report.commands), size):
        chunk = report.commands[start : start + size]
        overhead = statistics.median(item.host_overhead for item in chunk)
        preparing = statistics.median(item.preparing_context for item in chunk)
        lines.append(
            f"commands {chunk[0].step}-{chunk[-1].step}: host_overhead median "
            f"{overhead * 1000:.1f}ms, following preparing_context median {preparing * 1000:.1f}ms"
        )
    lines.append(
        f"reason={report.reason} commands={len(report.commands)} "
        f"failed_tool_results={report.failed_tool_results} wall={report.wall_seconds:.2f}s"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--commands", type=int, default=40)
    parser.add_argument("--json", type=Path, help="写出逐条明细与汇总")
    parser.add_argument("--profile", action="store_true", help="剖析事件循环线程并打印热点")
    arguments = parser.parse_args(argv)
    with TemporaryDirectory(prefix="harnessix-command-path-") as directory:
        root = Path(directory).resolve()

        def run() -> Coroutine[object, object, CommandPathReport]:
            return run_command_bench(root, commands=arguments.commands)

        report = profiled(run) if arguments.profile else asyncio.run(run())
    print(format_report(report))
    if arguments.json is not None:
        body = {
            "reason": report.reason,
            "failed_tool_results": report.failed_tool_results,
            "wall_seconds": report.wall_seconds,
            "summary": {name: list(values) for name, values in summary(report).items()},
            "commands": [
                asdict(item) | {"round_trip": item.round_trip, "host_overhead": item.host_overhead}
                for item in report.commands
            ],
            "steps": [asdict(item) | {"total": item.total} for item in report.steps],
        }
        arguments.json.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return 0 if report.valid and len(report.commands) == arguments.commands else 1


if __name__ == "__main__":
    sys.exit(main())
