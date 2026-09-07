from __future__ import annotations

import asyncio

import pytest

from harnessix.agent.errors import FailureCategory, KernelError
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer


def _parallel_step(*values: int):
    return [
        ResponseStarted(response_id="parallel"),
        *(
            ToolCallCompleted(call_id=f"call-{value}", tool="test.read", arguments={"value": value})
            for value in values
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


class ParallelReads(RecordingTools):
    def __init__(self, expected: int) -> None:
        super().__init__(parallel=True)
        self.expected = expected
        self.active = 0
        self.peak = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, call, cancel):
        cancel.checkpoint()
        self.calls.append(call)
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == self.expected:
            self.entered.set()
        try:
            await cancel.run(self.release.wait())
            return ToolResultContent(
                call_id=call.call_id,
                outcome="succeeded",
                output={"value": call.arguments["value"]},
            )
        finally:
            self.active -= 1


class FailingParallelReads(RecordingTools):
    def __init__(self) -> None:
        super().__init__(parallel=True)
        self.started = 0
        self.all_started = asyncio.Event()
        self.sibling_drained = asyncio.Event()

    async def execute(self, call, cancel):
        cancel.checkpoint()
        self.started += 1
        if self.started == 2:
            self.all_started.set()
        if call.arguments["value"] == 1:
            await self.all_started.wait()
            raise KernelError("tool_read_failed", "读取失败")
        try:
            await asyncio.Event().wait()
            raise AssertionError("不可达")
        finally:
            self.sibling_drained.set()


async def test_parallel_read_results_commit_in_provider_order(tmp_path) -> None:
    tools = ParallelReads(expected=2)
    provider = ScriptedProvider([_parallel_step(1, 2), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "并行读取", request_id="parallel")
        )
        await asyncio.wait_for(tools.entered.wait(), 10)
        assert tools.peak == 2
        tools.release.set()
        turn = await running
    assert turn.status is TurnStatus.COMPLETED
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert [result.output for result in results] == [{"value": 1}, {"value": 2}]


async def test_parallel_read_batch_obeys_kernel_limit(tmp_path) -> None:
    tools = ParallelReads(expected=2)
    provider = ScriptedProvider([_parallel_step(1, 2, 3), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"),
        provider,
        tools,
        max_parallel_tools=2,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "并发上限", request_id="bounded")
        )
        await asyncio.wait_for(tools.entered.wait(), 10)
        assert tools.peak == 2 and len(tools.calls) == 2
        tools.release.set()
        turn = await running
    assert turn.status is TurnStatus.COMPLETED
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert [result.output for result in results] == [
        {"value": 1},
        {"value": 2},
        {"value": 3},
    ]


async def test_turn_cancel_drains_all_parallel_reads(tmp_path) -> None:
    tools = ParallelReads(expected=2)
    provider = ScriptedProvider([_parallel_step(1, 2)])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "取消并行读取", request_id="cancel")
        )
        await asyncio.wait_for(tools.entered.wait(), 10)
        snapshot = await runtime.store.get_thread(thread.thread_id)
        assert snapshot.active_turn_id is not None
        cancelled = await runtime.cancel(thread.thread_id, snapshot.active_turn_id)
        turn = await running
    assert cancelled.status is TurnStatus.CANCELLING
    assert turn.status is TurnStatus.CANCELLED
    assert tools.active == 0


async def test_non_opt_in_reads_remain_serial_barrier(tmp_path) -> None:
    tools = ParallelReads(expected=1)
    tools.parallel = False
    provider = ScriptedProvider([_parallel_step(1, 2), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "串行读取", request_id="serial")
        )
        await asyncio.wait_for(tools.entered.wait(), 10)
        await asyncio.sleep(0.05)
        assert tools.peak == 1 and len(tools.calls) == 1
        tools.release.set()
        turn = await running
    assert turn.status is TurnStatus.COMPLETED
    assert len(tools.calls) == 2


async def test_parallel_failure_cancels_and_drains_siblings(tmp_path) -> None:
    tools = FailingParallelReads()
    provider = ScriptedProvider([_parallel_step(1, 2)])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await asyncio.wait_for(
            runtime.run_turn(thread.thread_id, "失败快停", request_id="fail-fast"), 10
        )
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None
    assert turn.error.code == "tool_read_failed"
    assert turn.error.category is FailureCategory.TOOL
    assert tools.sibling_drained.is_set()


@pytest.mark.parametrize("limit", [0, 17, True, 1.5])
def test_kernel_parallel_limit_is_bounded(tmp_path, limit) -> None:
    with pytest.raises(KernelError) as error:
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"),
            ScriptedProvider([]),
            max_parallel_tools=limit,
        )
    assert error.value.code == "tool_concurrency_invalid"


def test_parallel_capability_is_read_only_and_tool_failures_share_category() -> None:
    with pytest.raises(ValueError, match="只读"):
        ToolDescriptor(
            name="write",
            version="1",
            description="写",
            input_schema={"type": "object"},
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            requires_idempotency=True,
            requires_approval=True,
            supports_reconciliation=False,
            supports_parallel_calls=True,
        )
    for code in (
        "tool_invalid_arguments",
        "patch_source_changed",
        "process_timeout",
        "artifact_expired",
        "test_profile_not_found",
        "git_output_invalid",
        "workspace_changed",
    ):
        assert KernelError(code, "失败").to_failure().category is FailureCategory.TOOL
