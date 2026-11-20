"""长历史宿主开销基准：NL2Repo规模的脚本化Turn里，每个模型步骤的宿主开销随历史增长如何变化。

职责：以评测同款装配（产品配置、外部边界命令Profile、协议客户端自动审批）跑一个约160步、历史增长到
压缩触发点附近的Turn：从一份长说明出发逐个创建模块与测试文件、读取、编辑并运行命令，工具结果与
文件正文都是KB级。模型回复由脚本即时给出，每步耗时只剩宿主自身与工具执行。阶段边界取自Session
事件的时间戳（与评测诊断同一口径），按步骤分桶报告：preparing_context、waiting_approval，以及
calling_model内的前段（状态变为calling_model到模型尝试开始）、流式记录、尾段（最后一次用量到尝试
结束）与后段（尝试结束到下一次状态变化）。
非职责：不访问网络、不评价模型、不进入默认测试集（160步约数分钟）；不给产品代码加探针。

用法（在仓库根目录）：
    PYTHONPATH=src:. python scripts/long_history_overhead_bench.py --steps 160 --json out.json
    PYTHONPATH=src:. python scripts/long_history_overhead_bench.py --steps 160 --profile
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
from collections.abc import Coroutine, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.models import Usage
from harnessix.agent.usage import ModelUsageObserved, UsageObservation
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ToolCallCompleted,
)
from scripts.host_step_overhead_bench import (
    ClockedScriptedProvider,
    failed_tool_results,
    model_step,
    narration,
    profiled,
    run_scripted_turn,
)

PHASES = (
    "preparing_context",
    "pre",
    "stream",
    "tail",
    "post",
    "waiting_approval",
    "executing_tools",
)
# 一轮八步：建模块、看输出、读回、编辑、列目录、建测试、跑测试、读说明。
_CYCLE = ("create", "show", "read", "edit", "list", "test_file", "run_test", "spec")
_SPEC_SECTIONS = 80
_FUNCTIONS_PER_MODULE = 6
_TEST_CASES = 16
# 上报的输入Token按请求历史的JSON字节数加固定的工具与指令开销、每Token约3.6字节换算：与评测中代码
# 类历史的比例相近，校准后的压缩点因此落在真实窗口一半（约13万Token）附近。
_BYTES_PER_TOKEN = 3.6
_FIXED_REQUEST_BYTES = 24_000
_TIMED_STATES = frozenset({"preparing_context", "waiting_approval", "executing_tools"})


def _spec_section(index: int) -> str:
    return "\n".join(
        [
            f"## 3.{index} Component `ledgerkit.mod_{index:03d}`",
            "",
            f"The component reconciles ledger batch {index} against the warehouse snapshot. Each",
            "record carries an identifier, an amount in minor units and an optional memo. Records",
            "whose amount differs from the ledger by more than the tolerance are reported as",
            "missing; matched pairs keep the order of the input. The public functions are",
            f"`reconcile_{index}`, `summarise_{index}` and `validate_{index}`; each raises",
            "`LedgerError` with a stable message when its input is malformed.",
            "",
        ]
    )


def specification() -> str:
    """固定内容的长说明（约40 KB）：既是Turn的说明，也是工作区里可读的SPEC.md。"""
    head = [
        "# ledgerkit specification",
        "",
        "Implement the package `ledgerkit` from this specification. Every module lives in",
        "`pkg/`, every module has a test file in `tests/`, and the tests must pass.",
        "",
    ]
    return "\n".join(head + [_spec_section(index) for index in range(_SPEC_SECTIONS)])


def _module_source(index: int) -> str:
    lines = [f'"""ledgerkit.mod_{index:03d}: batch {index} reconciliation."""', ""]
    for function in range(_FUNCTIONS_PER_MODULE):
        lines += [
            f"def reconcile_{index}_{function}(records, ledger, tolerance=0.01):",
            f'    """Reconcile part {function} of batch {index} against the ledger."""',
            "    matched, missing = [], []",
            "    for record in records:",
            "        entry = ledger.get(record.identifier)",
            "        if entry is None or abs(entry.amount - record.amount) > tolerance:",
            "            missing.append(record)",
            "        else:",
            "            matched.append((record, entry))",
            f"    return matched, missing  # step-{index:03d}-{function}",
            "",
        ]
    return "\n".join(lines)


def _test_source(index: int) -> str:
    lines = [f'"""Checks for ledgerkit.mod_{index:03d}."""', ""]
    for case in range(_TEST_CASES):
        lines.append(f'print("tests/test_mod_{index:03d}.py::test_case_{case:02d} PASSED")')
    lines.append(f'print("{_TEST_CASES} passed in 0.{index % 90 + 10}s")')
    return "\n".join(lines) + "\n"


def seed_long_workspace(workspace: Path) -> None:
    for directory in ("pkg", "tests"):
        (workspace / directory).mkdir()
    (workspace / "SPEC.md").write_text(specification(), encoding="utf-8")


def _create(path: str, content: str) -> tuple[str, dict[str, object]]:
    entry = {"operation": "create", "path": path, "content": content, "mode": 420}
    return "apply_patch_batch", {"files": [entry]}


def tool_call(kind: str, step: int) -> tuple[str, dict[str, object]]:
    """第step步的工具调用；同一轮八步围绕同一个模块编号。"""
    index = (step - 1) // len(_CYCLE)
    module, test = f"pkg/mod_{index:03d}.py", f"tests/test_mod_{index:03d}.py"
    if kind == "create":
        return _create(module, _module_source(index))
    if kind == "show":
        return "exec_command.testbed", {"cmd": f"sed -n '1,50p' {module}"}
    if kind == "read":
        return "read_file", {"path": module, "start_line": 1, "max_lines": 60}
    if kind == "edit":
        marker = f"    return matched, missing  # step-{index:03d}-0"
        edit = {"old_text": marker, "new_text": marker.replace("step-", "checked-")}
        return "apply_patch_batch", {
            "files": [{"operation": "edit", "path": module, "edits": [edit]}]
        }
    if kind == "list":
        listing = "ls -la pkg tests | tail -n 30 && wc -c pkg/*.py | tail -n 1"
        return "exec_command.testbed", {"cmd": listing}
    if kind == "test_file":
        return _create(test, _test_source(index))
    if kind == "run_test":
        return "exec_command.testbed", {"cmd": f"python3 {test}"}
    start = 1 + (index * 10) % (_SPEC_SECTIONS * 10)
    return "read_file", {"path": "SPEC.md", "start_line": start, "max_lines": 30}


def build_long_script(steps: int) -> tuple[list[list[ProviderEvent]], list[str]]:
    """steps个工具步骤加一个纯文本收尾；上报的输入Token由SizedScriptedProvider按请求改写。"""
    script: list[list[ProviderEvent]] = []
    tools: list[str] = []
    for step in range(1, steps + 1):
        kind = _CYCLE[(step - 1) % len(_CYCLE)]
        tool, arguments = tool_call(kind, step)
        tools.append(tool)
        text = (
            f"Step {step}: the specification asks for {kind} evidence before the next module, "
            "so I keep the batch layout identical and verify it before moving on."
        )
        call = ToolCallCompleted(call_id=f"long-call-{step}", tool=tool, arguments=arguments)
        script.append(model_step(step, [*narration(step, text), call]))
    summary = "All modules and tests follow the specification and the test files pass."
    script.append(model_step(steps + 1, narration(steps + 1, summary)))
    return script, tools


class SizedScriptedProvider(ClockedScriptedProvider):
    """按请求实际携带的历史上报输入Token：压缩后历史变短，上报的用量随之变小，与真实模型一致。

    每个历史Item的JSON字节数按Item ID只算一次，每步只为新增的几项序列化。
    """

    def __init__(self, steps: Sequence[Sequence[ProviderEvent]], *, delay_seconds: float) -> None:
        super().__init__(steps, delay_seconds=delay_seconds)
        self._item_bytes: dict[object, int] = {}

    def events_for(self, request: ModelRequest) -> Sequence[ProviderEvent]:
        events = super().events_for(request)
        sizes = self._item_bytes
        for item in request.history:
            if item.item_id not in sizes:
                sizes[item.item_id] = len(item.model_dump_json().encode())
        total = _FIXED_REQUEST_BYTES + sum(sizes[item.item_id] for item in request.history)
        tokens = int(total / _BYTES_PER_TOKEN)
        return [_with_input_tokens(event, tokens) for event in events]


def _with_input_tokens(event: ProviderEvent, tokens: int) -> ProviderEvent:
    if isinstance(event, ModelUsageObserved):
        usage = event.usage.model_copy(update={"input_tokens": tokens})
        return event.model_copy(update={"usage": UsageObservation.model_validate(usage)})
    if isinstance(event, ResponseCompleted) and event.usage is not None:
        return event.model_copy(
            update={"usage": Usage(input_tokens=tokens, output_tokens=event.usage.output_tokens)}
        )
    return event


@dataclass
class StepPhases:
    """一个模型步骤内各阶段的秒数，阶段边界取自Session事件的时间戳。"""

    step: int
    tool: str = ""
    preparing_context: float = 0.0
    pre: float = 0.0
    stream: float = 0.0
    tail: float = 0.0
    post: float = 0.0
    waiting_approval: float = 0.0
    executing_tools: float = 0.0
    estimated_input_bytes: int = 0
    session_events: int = 0
    cpu: float = 0.0
    compaction: bool = False
    usage_at: list[float] = field(default_factory=list, repr=False)

    @property
    def host(self) -> float:
        """宿主开销：除工具执行外的全部阶段（审批往返含协议客户端的处理）。"""
        return sum(getattr(self, phase) for phase in PHASES if phase != "executing_tools")


def _moment(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def session_phases(sessions: Path, tools: Sequence[str]) -> list[StepPhases]:
    """从sessions.db的事件按模型步骤切分阶段；步骤以进入calling_model之后的第一次preparing_context
    开始，收尾的纯文本步骤不计入。压缩事件（含其中包着的摘要请求）只标记所在步骤，不计入各阶段。"""
    with sqlite3.connect(f"file:{sessions}?mode=ro", uri=True) as database:
        rows = [
            json.loads(text)
            for (text,) in database.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ]
    steps: list[StepPhases] = []
    state: str | None = None
    state_at = 0.0
    called = True
    finished_at: float | None = None
    for row in rows:
        payload, at = row["payload"], _moment(row["occurred_at"])
        kind = payload["type"]
        current = steps[-1] if steps else None
        if current is not None:
            current.session_events += 1
            current.compaction = current.compaction or kind.startswith("compaction_")
        if kind == "turn_state_changed":
            status = payload["status"]
            if current is not None and state in _TIMED_STATES:
                setattr(current, state, getattr(current, state) + at - state_at)
            if current is not None and finished_at is not None:
                current.post += at - finished_at
                finished_at = None
            if status == "preparing_context" and called:
                if len(steps) == len(tools):
                    break
                steps.append(StepPhases(step=len(steps) + 1, tool=tools[len(steps)]))
                called = False
            called = called or status == "calling_model"
            state, state_at = status, at
        elif current is None:
            continue
        elif kind == "model_attempt_started" and state == "calling_model":
            current.pre += at - state_at
            state_at = at
        elif kind == "model_usage_observed":
            current.usage_at.append(at)
        elif kind == "model_attempt_finished" and current.usage_at:
            current.stream += current.usage_at[-1] - state_at
            current.tail += at - current.usage_at[-1]
            finished_at = at
        elif kind == "context_prepared":
            current.estimated_input_bytes = payload["inspection"]["estimated_input_tokens"]
    return steps


@dataclass(frozen=True)
class LongReport:
    steps: tuple[StepPhases, ...]
    wall_seconds: float
    reason: str
    failed_tool_results: int
    summaries: int

    @property
    def valid(self) -> bool:
        return self.reason == "completed" and self.failed_tool_results == 0

    def buckets(self, size: int = 40) -> list[dict[str, float | str]]:
        """按步骤序号分桶，各阶段取桶内不含压缩的步骤的平均；压缩步骤只计数。"""
        rows: list[dict[str, float | str]] = []
        for start in range(0, len(self.steps), size):
            chunk = self.steps[start : start + size]
            plain = [item for item in chunk if not item.compaction] or chunk
            row: dict[str, float | str] = {"steps": f"{chunk[0].step}-{chunk[-1].step}"}
            for phase in PHASES:
                row[phase] = statistics.fmean(getattr(item, phase) for item in plain)
            row["host_mean"] = statistics.fmean(item.host for item in plain)
            row["host_median"] = statistics.median(item.host for item in plain)
            row["cpu_mean"] = statistics.fmean(item.cpu for item in plain)
            row["input_kb_mean"] = statistics.fmean(i.estimated_input_bytes for i in plain) / 1024
            row["input_kb_max"] = max(item.estimated_input_bytes for item in chunk) / 1024
            row["session_events"] = statistics.fmean(item.session_events for item in plain)
            row["compaction_steps"] = len(chunk) - len(plain) if plain is not chunk else 0
            rows.append(row)
        return rows


async def run_long_benchmark(root: Path, *, steps: int = 160) -> LongReport:
    script, tools = build_long_script(steps)
    turn = await run_scripted_turn(
        root,
        script,
        instruction=specification(),
        seed=seed_long_workspace,
        provider_type=SizedScriptedProvider,
    )
    phases = session_phases(turn.state / "sessions.db", tools)
    for index, item in enumerate(phases):
        if index + 1 < len(turn.cpu_marks):
            item.cpu = turn.cpu_marks[index + 1] - turn.cpu_marks[index]
    return LongReport(
        steps=tuple(phases),
        wall_seconds=turn.wall_seconds,
        reason=turn.reason,
        failed_tool_results=failed_tool_results(turn.events),
        summaries=turn.summaries,
    )


def format_report(report: LongReport, size: int = 40) -> str:
    columns = (*PHASES, "host_mean", "host_median", "cpu_mean")
    lines = [
        f"{'steps':>8} "
        + " ".join(f"{name[:12]:>12}" for name in columns)
        + "  input_kb(mean/max) compactions"
    ]
    for row in report.buckets(size):
        cells = " ".join(f"{float(row[name]) * 1000:>10.1f}ms" for name in columns)
        sizes = f"{float(row['input_kb_mean']):>8.0f}/{float(row['input_kb_max']):.0f}"
        lines.append(f"{row['steps']:>8} {cells} {sizes} {row['compaction_steps']:>6}")
    count = max(len(report.steps), 1)
    host = sum(item.host for item in report.steps)
    lines.append(
        f"reason={report.reason} steps={len(report.steps)} "
        f"failed_tool_results={report.failed_tool_results} summaries={report.summaries} "
        f"host_per_step={host / count * 1000:.0f}ms wall={report.wall_seconds:.1f}s"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--steps", type=int, default=160)
    parser.add_argument("--bucket", type=int, default=40)
    parser.add_argument("--json", type=Path, help="写出逐步明细与分桶结果")
    parser.add_argument("--profile", action="store_true", help="剖析事件循环线程并打印热点")
    arguments = parser.parse_args(argv)
    with TemporaryDirectory(prefix="harnessix-long-") as directory:
        root = Path(directory).resolve()

        def run() -> Coroutine[object, object, LongReport]:
            return run_long_benchmark(root, steps=arguments.steps)

        report = profiled(run) if arguments.profile else asyncio.run(run())
    print(format_report(report, arguments.bucket))
    if arguments.json is not None:
        body = {
            "reason": report.reason,
            "failed_tool_results": report.failed_tool_results,
            "summaries": report.summaries,
            "wall_seconds": report.wall_seconds,
            "buckets": report.buckets(arguments.bucket),
            "steps": [
                {key: value for key, value in asdict(item).items() if key != "usage_at"}
                | {"host": item.host}
                for item in report.steps
            ],
        }
        arguments.json.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return 0 if report.valid and len(report.steps) == arguments.steps else 1


if __name__ == "__main__":
    sys.exit(main())
