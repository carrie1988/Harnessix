"""收尾之后不再发起新的修改或检查（Beta Issue #4）。

0.10.0b24 macOS：测试刚通过、模型正准备下一步时按Ctrl+F，界面显示收尾说明已进入会话，
模型仍读了README、跑了`--help`与缺参检查，53秒后才结束。说明只在下一步被模型看到，模型也可以不照做。
现在宿主保证：说明记入本轮之后，尚未开始的命令、修改等经审批边界的调用不执行（如实回给模型），
之后的步骤不提供任何工具。同一步里已提出的只读读取照常完成（毫秒级，不经审批边界）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import TextContent, ToolResultContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ModelRequest, ProviderEvent
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.wrap_up import (
    WRAP_UP_NOTICE,
    WRAP_UP_PROMPT,
    WRAP_UP_STEER,
    WrapUpToolView,
)
from harnessix.product_ui.turn_checkpoint import WRAP_UP_PROMPT as CHECKPOINT_WRAP_UP
from harnessix.product_ui.turn_pacing import WRAP_UP_MESSAGE
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step


class _HeldFirstStep(ScriptedProvider):
    """第1步的回复（一个读取调用）先扣住，等测试在“模型正在生成”时补充说明后再放出。"""

    def __init__(self) -> None:
        super().__init__([tool_step("test.read"), answer("总结：测试已全部通过。")])
        self.calling = asyncio.Event()
        self.turn_id = None
        self.release = asyncio.Event()

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        if request.step == 1:
            self.turn_id = request.turn_id
            self.calling.set()
            await self.release.wait()
        async for event in super().stream(request, cancel):
            yield event


async def _run(
    tmp_path: Path, *, steer: str | None, approval: bool = True
) -> tuple[object, RecordingTools, _HeldFirstStep]:
    # 经审批边界的调用代表命令与修改（产品里的检查命令都是这一类）。
    tools, provider = RecordingTools(approval=approval), _HeldFirstStep()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"), provider, tools, model_tool_view=WrapUpToolView()
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "检查并汇报", request_id="r")
        )
        await asyncio.wait_for(provider.calling.wait(), 10)
        if steer is not None:
            await runtime.steer_turn(
                thread.thread_id, provider.turn_id, steer, request_id="wrap-up"
            )
        provider.release.set()
        turn = await asyncio.wait_for(running, 30)
    return turn, tools, provider


async def test_wrap_up_while_the_model_is_answering_runs_nothing_more(tmp_path: Path) -> None:
    turn, tools, provider = await _run(tmp_path, steer=WRAP_UP_STEER)

    assert turn.status.value == "completed", turn.error
    # 模型在看到说明之前提出的读取没有执行，模型收到的是如实的说明。
    assert tools.calls == []
    (refused,) = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert refused.outcome == "failed" and refused.error is not None
    assert refused.error.code == "tool_wrap_up_requested"
    # 下一步不提供任何工具，只能给出总结。
    assert [len(request.tools) for request in provider.requests] == [1, 0]
    replies = [
        i.content.text
        for i in turn.items
        if isinstance(i.content, TextContent) and i.content.kind == "assistant_message"
    ]
    assert replies[-1] == "总结：测试已全部通过。"


async def test_a_read_already_proposed_still_finishes_but_nothing_follows(tmp_path: Path) -> None:
    """不经审批边界的只读读取在这一步照常完成；之后照样不提供工具，直接总结。"""
    turn, tools, provider = await _run(tmp_path, steer=WRAP_UP_STEER, approval=False)

    assert turn.status.value == "completed", turn.error
    assert [call.tool for call in tools.calls] == ["test.read"]
    assert [len(request.tools) for request in provider.requests] == [1, 0]


async def test_without_wrap_up_the_same_turn_runs_its_calls_as_before(tmp_path: Path) -> None:
    turn, tools, provider = await _run(tmp_path, steer=None, approval=False)

    assert turn.status.value == "completed", turn.error
    assert [call.tool for call in tools.calls] == ["test.read"]
    assert [len(request.tools) for request in provider.requests] == [1, 1]


async def test_another_steer_does_not_stop_the_work(tmp_path: Path) -> None:
    turn, tools, provider = await _run(tmp_path, steer="顺便看看README", approval=False)

    assert [call.tool for call in tools.calls] == ["test.read"]
    assert [len(request.tools) for request in provider.requests] == [1, 1]


async def test_a_turn_started_with_the_checkpoint_wrap_up_offers_no_tools(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer("总结")])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        provider,
        RecordingTools(),
        model_tool_view=WrapUpToolView(),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, WRAP_UP_PROMPT, request_id="r")
    assert turn.status.value == "completed"
    assert [len(request.tools) for request in provider.requests] == [0]
    # 0.10.0b36：只有空目录时，百炼qwen3.8-flash约五分之一的情况下仍连续4～6次调用工具。
    # 目录说明写明是用户要求收尾、调用不会执行、应直接总结之后，同一场景24次都直接给出总结。
    assert [request.tool_notice for request in provider.requests] == [WRAP_UP_NOTICE]


@pytest.mark.parametrize("wrap_up", [False, True], ids=["working", "wrapping-up"])
async def test_with_notes_in_history_only_a_wrap_up_adds_a_catalog_note(
    tmp_path: Path, wrap_up: bool
) -> None:
    """说明随历史持久化的端点（稳定前缀，ADR 0132）：收尾视图始终安装，没有收尾时不给每一轮开头
    添一条目录说明（模型输入与没有视图时相同）；收尾之后的说明告诉模型不再有工具可用。"""
    provider = ScriptedProvider([answer("总结")])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        provider,
        RecordingTools(),
        model_tool_view=WrapUpToolView(),
        history_notes=True,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        prompt = WRAP_UP_PROMPT if wrap_up else "检查并汇报"
        await runtime.run_turn(thread.thread_id, prompt, request_id="r")
    notes = [
        item.content.text
        for item in provider.requests[0].history
        if isinstance(item.content, TextContent)
        and "Callable tools from now on" in item.content.text
    ]
    assert len(notes) == (1 if wrap_up else 0)
    if wrap_up:
        assert "No tools are callable." in notes[0] and WRAP_UP_NOTICE in notes[0]


@pytest.mark.parametrize(
    ("ui_text", "shared"),
    [(WRAP_UP_MESSAGE, WRAP_UP_STEER), (CHECKPOINT_WRAP_UP, WRAP_UP_PROMPT)],
    ids=["ctrl-f", "checkpoint"],
)
def test_the_interface_sends_exactly_the_texts_the_host_recognises(ui_text, shared) -> None:  # type: ignore[no-untyped-def]
    assert ui_text == shared
