"""宿主每步开销基准：用脚本化模型驱动真实产品运行时，按历史长度报告各阶段耗时。

职责：在临时工作区里以评测同款装配（产品配置、外部边界命令Profile、协议客户端自动审批）跑一个
多步Turn。模型回复由ScriptedProvider即时给出，所以每步耗时只剩宿主自身与工具执行；再从公开事件的
时间戳按步拆出各阶段。非职责：不访问网络、不评价模型、不进入默认测试集（60步约一分钟）。

用法（在仓库根目录）：
    PYTHONPATH=src:. python scripts/host_step_overhead_bench.py --steps 60 --json out.json
    PYTHONPATH=src:. python scripts/host_step_overhead_bench.py --steps 60 --profile
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import statistics
import sys
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import FrameType
from uuid import uuid4

from harnessix.agent.models import Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.app_server.server import AgentProtocolServer
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config import server as product
from harnessix.product_config.native_process import EXTERNAL_BOUNDARY_ENVIRONMENT
from harnessix.product_config.runtime import build_provider_bundle
from harnessix.protocol.contracts import PublicBudget, PublicEvent
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from integrations.harbor.harnessix_configs import action_config, product_config
from integrations.harbor.protocol_runner import drive_protocol_trial
from integrations.harbor.task_environment import API_KEY_VARIABLE

PHASES = ("preparing_context", "model_call", "after_usage", "approval", "tool_and_result")
_MODULES = 24
_FUNCTIONS_PER_MODULE = 40
_LINES_PER_FUNCTION = 11
_TEXT_DELTAS = 12
# 评测中的工具占比约为：命令39%、grep 27%、read_file 22%、补丁11%。
_STEP_CYCLE = ("grep", "read", "exec", "grep", "read", "exec", "patch", "exec", "grep")


@dataclass(frozen=True)
class StepTiming:
    """一个模型步骤内各阶段的秒数；阶段边界取自公开事件的时间戳。

    model_call是进入calling_model到用量事件之间的时间：脚本化模型不耗时，剩下的是宿主记录回复的开销
    （设置了增量间隔时已减去脚本的等待）。tool_and_result含工具本身的执行时间。
    """

    step: int
    tool: str
    preparing_context: float
    model_call: float
    after_usage: float
    approval: float
    tool_and_result: float

    @property
    def total(self) -> float:
        return (
            self.preparing_context
            + self.model_call
            + self.after_usage
            + self.approval
            + self.tool_and_result
        )


@dataclass(frozen=True)
class BenchReport:
    steps: tuple[StepTiming, ...]
    wall_seconds: float
    reason: str
    failed_tool_results: int
    session_bytes: int

    @property
    def valid(self) -> bool:
        """只有Turn正常完成且每个工具调用都成功，耗时才代表预期的工作量。"""
        return self.reason == "completed" and self.failed_tool_results == 0

    def buckets(self, size: int = 10) -> list[dict[str, float | str]]:
        """按步骤序号分桶：各阶段取桶内平均（相加即每步平均），历史越长的桶越靠后。"""
        rows: list[dict[str, float | str]] = []
        for start in range(0, len(self.steps), size):
            chunk = self.steps[start : start + size]
            row: dict[str, float | str] = {"steps": f"{chunk[0].step}-{chunk[-1].step}"}
            for phase in PHASES:
                row[phase] = statistics.fmean(getattr(item, phase) for item in chunk)
            row["total_mean"] = statistics.fmean(item.total for item in chunk)
            row["total_median"] = statistics.median(item.total for item in chunk)
            rows.append(row)
        return rows


def _module_source(index: int) -> str:
    lines = [
        f'"""Inventory module {index}: deterministic fixture for the overhead benchmark."""',
        "",
    ]
    for function in range(_FUNCTIONS_PER_MODULE):
        lines += [
            f"def reconcile_batch_{index}_{function}(records, ledger, tolerance=0.01):",
            f'    """Reconcile batch {function} of warehouse {index} against the ledger."""',
            "    matched, missing = [], []",
            "    for record in records:",
            "        entry = ledger.get(record.identifier)",
            "        if entry is None or abs(entry.amount - record.amount) > tolerance:",
            "            missing.append(record)",
            "        else:",
            "            matched.append((record, entry))",
            f"    return matched, missing  # marker-{index:02d}-{function:02d}",
            "",
        ]
    return "\n".join(lines)


def seed_workspace(workspace: Path) -> None:
    """生成固定内容的源码树：读取与搜索的结果多为1–3 KB，每个补丁锚点在文件内唯一。"""
    package = workspace / "inventory"
    package.mkdir(parents=True)
    for index in range(_MODULES):
        (package / f"warehouse_{index:02d}.py").write_text(_module_source(index), encoding="utf-8")


def _tool_call(kind: str, step: int) -> tuple[str, dict[str, object]]:
    module = step % _MODULES
    function = step % _FUNCTIONS_PER_MODULE
    path = f"inventory/warehouse_{module:02d}.py"
    if kind == "grep":
        return "grep", {"path": "inventory", "query": f"reconcile_batch_{module}_{function % 10}"}
    if kind == "read":
        start = 1 + _LINES_PER_FUNCTION * function
        return "read_file", {"path": path, "start_line": start, "max_lines": 40}
    if kind == "exec":
        command = f"sed -n '{1 + step},{30 + step}p' {path} && wc -l {path}"
        return "exec_command.testbed", {"cmd": command}
    marker = f"    return matched, missing  # marker-{module:02d}-{function:02d}"
    edit = {"old_text": marker, "new_text": marker.replace("marker", "patched")}
    return "apply_patch_batch", {"files": [{"operation": "edit", "path": path, "edits": [edit]}]}


def _narration(step: int, text: str) -> list[ProviderEvent]:
    content = f"text-{step}"
    size = -(-len(text) // _TEXT_DELTAS)
    deltas = [text[offset : offset + size] for offset in range(0, len(text), size)]
    return [
        TextStarted(content_id=content),
        *(TextDelta(content_id=content, delta=delta) for delta in deltas),
        TextCompleted(content_id=content, text=text),
    ]


def _model_step(step: int, events: Sequence[ProviderEvent]) -> list[ProviderEvent]:
    """按真实Provider的顺序包上尝试、用量与回复终态。"""
    attempt = uuid4()
    usage = Usage(input_tokens=2_000 + 600 * step, output_tokens=180)
    calls = any(isinstance(event, ToolCallCompleted) for event in events)
    return [
        ModelAttemptStarted(
            attempt_id=attempt, step=step, index=1, provider="openai_chat", requested_model="bench"
        ),
        ResponseStarted(response_id=f"bench-response-{step}"),
        *events,
        ModelUsageObserved(
            attempt_id=attempt,
            actual_model="bench",
            response_id=f"bench-response-{step}",
            usage=UsageObservation(
                completeness="complete",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
        ),
        ModelAttemptFinished(attempt_id=attempt, outcome="completed"),
        ResponseCompleted(finish_reason="tool_calls" if calls else "completed", usage=usage),
    ]


def build_script(steps: int) -> tuple[list[list[ProviderEvent]], list[str]]:
    """steps个工具步骤加一个纯文本收尾；每步一句说明加一个工具调用。"""
    script: list[list[ProviderEvent]] = []
    tools: list[str] = []
    for step in range(1, steps + 1):
        kind = _STEP_CYCLE[(step - 1) % len(_STEP_CYCLE)]
        tool, arguments = _tool_call(kind, step)
        tools.append(tool)
        text = (
            f"Step {step}: the reconciliation path still loses records when the ledger entry "
            f"is absent, so I check the {kind} evidence for warehouse {step % _MODULES} next."
        )
        call = ToolCallCompleted(call_id=f"bench-call-{step}", tool=tool, arguments=arguments)
        script.append(_model_step(step, [*_narration(step, text), call]))
    summary = "The reconciliation markers are updated and the affected modules were re-read."
    script.append(_model_step(steps + 1, _narration(steps + 1, summary)))
    return script, tools


def step_timings(
    events: Sequence[PublicEvent], tools: Sequence[str], scripted_wait_seconds: Sequence[float]
) -> tuple[StepTiming, ...]:
    """把公开事件切成模型步骤：每步从preparing_context开始，到下一次preparing_context之前结束。"""
    timings: list[StepTiming] = []
    phases = dict.fromkeys(PHASES, 0.0)
    current: str | None = None
    previous = 0.0

    def close_step() -> None:
        step = len(timings) + 1
        phases["model_call"] = max(0.0, phases["model_call"] - scripted_wait_seconds[step - 1])
        timings.append(StepTiming(step=step, tool=tools[step - 1], **phases))

    for event in events:
        moment = event.occurred_at.timestamp()
        data = event.data
        status = getattr(data, "status", None) if data.type == "turn_state_changed" else None
        if current is not None:
            phases[current] += moment - previous
        previous = moment
        if status == "preparing_context":
            if current is not None and len(timings) < len(tools):
                close_step()
            phases = dict.fromkeys(PHASES, 0.0)
            current = "preparing_context"
        elif status == "calling_model":
            current = "model_call"
        elif data.type == "usage_updated":
            current = "after_usage"
        elif status == "waiting_approval":
            current = "approval"
        elif status == "executing_tools":
            current = "tool_and_result"
        elif status is not None:
            current = None
    return tuple(timings)


def _failed_tool_results(events: Sequence[PublicEvent]) -> int:
    failed = 0
    for event in events:
        if event.data.type != "item_finished":
            continue
        content = event.data.item.content
        if content.kind == "tool_result" and content.outcome != "succeeded":
            failed += 1
    return failed


async def run_benchmark(
    root: Path, *, steps: int = 60, delta_interval_seconds: float = 0.0
) -> BenchReport:
    """在root下建工作区与状态目录并跑完一个Turn；root须为已存在的空目录，且不在任何工作区内。

    delta_interval_seconds大于0时脚本在每个模型事件前等待这么久，用来观察流式输出期间客户端轮询
    对宿主的占用；等待本身从model_call中减去。
    """
    workspace, state = root / "workspace", root / "state"
    workspace.mkdir()
    seed_workspace(workspace)
    config, actions = root / "product.json", root / "actions.json"
    config.write_text(product_config(8000), encoding="utf-8")
    environment = (("HOME", str(root)), ("PATH", os.environ.get("PATH", "/usr/bin:/bin")))
    actions.write_text(action_config(environment=environment), encoding="utf-8")
    for path in (config, actions):
        path.chmod(0o600)
    script, tools = build_script(steps)
    provider = ScriptedProvider(script, delay_seconds=delta_interval_seconds)
    events: list[PublicEvent] = []
    receipts: list[dict[str, object]] = []

    async def build(snapshot, selection, secrets, *, audit):  # type: ignore[no-untyped-def]
        return await build_provider_bundle(
            snapshot, selection, secrets, audit=audit, factory=lambda *_: provider
        )

    async def drive(server: AgentProtocolServer, _input: object, _output: object) -> None:
        client = AgentClient(InProcessAgentTransport(server))
        try:
            receipts.append(
                await drive_protocol_trial(
                    client,
                    workspace=str(workspace),
                    instruction="Reconcile the inventory markers and verify the affected modules.",
                    budget=PublicBudget(
                        max_steps=steps + 8,
                        max_tokens=50_000_000,
                        timeout_seconds=3600,
                        max_output_chars=65_536,
                        max_tool_calls_per_step=32,
                    ),
                    trial_id=uuid4(),
                    approval_mode="auto",
                    events=events,
                )
            )
        finally:
            await client.close()

    names = (API_KEY_VARIABLE, EXTERNAL_BOUNDARY_ENVIRONMENT)
    saved_environment = {name: os.environ.get(name) for name in names}
    saved_seams = product.build_provider_bundle, product.run_stdio
    os.environ[API_KEY_VARIABLE] = "bench-" + "k" * 24
    os.environ[EXTERNAL_BOUNDARY_ENVIRONMENT] = "1"
    product.build_provider_bundle, product.run_stdio = build, drive
    started = perf_counter()
    try:
        await product.run_product_stdio(
            config_path=config,
            action_config_path=actions,
            profile_id=None,
            workspace=workspace,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    finally:
        product.build_provider_bundle, product.run_stdio = saved_seams
        for name, value in saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    wall = perf_counter() - started
    (receipt,) = receipts
    sessions = state / "sessions.db"
    return BenchReport(
        steps=step_timings(
            events, tools, [len(step) * delta_interval_seconds for step in script[:steps]]
        ),
        wall_seconds=wall,
        reason=str(receipt["reason"]),
        failed_tool_results=_failed_tool_results(events),
        session_bytes=sessions.stat().st_size if sessions.is_file() else 0,
    )


def format_report(report: BenchReport) -> str:
    columns = (*PHASES, "total_mean", "total_median")
    lines = [f"{'steps':>7} " + " ".join(f"{name:>17}" for name in columns)]
    for row in report.buckets():
        cells = " ".join(f"{float(row[name]) * 1000:>15.1f}ms" for name in columns)
        lines.append(f"{row['steps']:>7} {cells}")
    total = sum(item.total for item in report.steps)
    lines.append(
        f"reason={report.reason} steps={len(report.steps)} "
        f"failed_tool_results={report.failed_tool_results} step_time_total={total:.2f}s "
        f"per_step={total / max(len(report.steps), 1) * 1000:.0f}ms "
        f"wall={report.wall_seconds:.2f}s sessions.db={report.session_bytes / 1024:.0f}KB"
    )
    return "\n".join(lines)


class StackSampler(threading.Thread):
    """按固定间隔采样全部线程的调用栈：SQLite读写在各连接的工作线程里，单线程Profiler看不到。

    一个样本代表某线程约一个间隔的墙钟时间；空闲等待（事件循环select、队列与锁等待）不计。
    """

    _IDLE = frozenset({"select", "wait", "get", "_worker", "run_forever", "sleep"})

    def __init__(self, interval_seconds: float = 0.002) -> None:
        super().__init__(name="overhead-sampler", daemon=True)
        self.interval_seconds = interval_seconds
        self.inclusive: Counter[tuple[str, str]] = Counter()
        self.leaves: Counter[tuple[str, str]] = Counter()
        self.samples = 0
        self._finished = threading.Event()

    def run(self) -> None:
        own = threading.get_ident()
        while not self._finished.wait(self.interval_seconds):
            for ident, frame in sys._current_frames().items():
                if ident == own or frame.f_code.co_name in self._IDLE:
                    continue
                self.samples += 1
                self.leaves[(frame.f_code.co_filename, frame.f_code.co_name)] += 1
                seen: set[tuple[str, str]] = set()
                current: FrameType | None = frame
                while current is not None:
                    seen.add((current.f_code.co_filename, current.f_code.co_name))
                    current = current.f_back
                self.inclusive.update(seen)

    def stop(self) -> None:
        self._finished.set()
        self.join()

    def table(self, limit: int = 40) -> str:
        """产品代码按包含样本排序，另列叶子帧（实际消耗CPU或阻塞在C调用的位置）。"""

        def rows(counter: Counter[tuple[str, str]], only_product: bool) -> list[str]:
            selected = [
                (count, filename, name)
                for (filename, name), count in counter.most_common()
                if not only_product or "/harnessix/" in filename
            ][:limit]
            return [
                f"{count * self.interval_seconds:8.2f}s {count / max(self.samples, 1):6.1%}  "
                f"{Path(filename).parent.name}/{Path(filename).name}:{name}"
                for count, filename, name in selected
            ]

        busy = self.samples * self.interval_seconds
        return "\n".join(
            [
                f"busy thread samples: {self.samples} (~{busy:.1f}s)",
                "-- inclusive, product code --",
                *rows(self.inclusive, True),
                "-- leaf frames --",
                *rows(self.leaves, False),
            ]
        )


def _profiled(root: Path, steps: int, delta_interval_seconds: float) -> BenchReport:
    sampler = StackSampler()
    sampler.start()
    try:
        return asyncio.run(
            run_benchmark(root, steps=steps, delta_interval_seconds=delta_interval_seconds)
        )
    finally:
        sampler.stop()
        print(sampler.table())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--steps", type=int, default=60)
    parser.add_argument("--delta-interval-ms", type=float, default=0.0, help="模型事件之间的等待")
    parser.add_argument("--json", type=Path, help="写出逐步明细与分桶结果")
    parser.add_argument("--profile", action="store_true", help="采样全部线程并打印热点")
    arguments = parser.parse_args(argv)
    interval = arguments.delta_interval_ms / 1000
    with TemporaryDirectory(prefix="harnessix-overhead-") as directory:
        root = Path(directory).resolve()
        if arguments.profile:
            report = _profiled(root, arguments.steps, interval)
        else:
            report = asyncio.run(
                run_benchmark(root, steps=arguments.steps, delta_interval_seconds=interval)
            )
    print(format_report(report))
    if arguments.json is not None:
        body = {
            "reason": report.reason,
            "failed_tool_results": report.failed_tool_results,
            "wall_seconds": report.wall_seconds,
            "session_bytes": report.session_bytes,
            "buckets": report.buckets(),
            "steps": [asdict(item) | {"total": item.total} for item in report.steps],
        }
        arguments.json.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return 0 if report.valid and len(report.steps) == arguments.steps else 1


if __name__ == "__main__":
    sys.exit(main())
