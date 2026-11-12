"""模型请求进行中的生成进度：适配器上报、运行时节流发布、协商过滤、界面的阶段与用量估算。

真实试用（0.10.0b20，百炼qwen3.8-flash）：第一次请求持续10分钟，其中思考2.8万Token、随后整段生成
写文件的参数；界面只有静止的“思考中”，Token与费用一直是0，用户两次问“是不是卡死了”。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from openai.types.chat import ChatCompletionChunk

from harnessix.agent.generation_progress import (
    GenerationCounts,
    GenerationProgress,
    generation_progress_item_id,
    latest_counts,
)
from harnessix.agent.models import ItemDelta
from harnessix.agent.reasoning_preview import reasoning_item_id
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.preview_streams import NO_PREVIEWS, PreviewStreams
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models._anthropic_stream import AnthropicStream, validate_event
from harnessix.models._chat_stream import ChatStream
from harnessix.models._history import tool_alias
from harnessix.models._progress import PROGRESS_STEP_BYTES, ArgumentProgress
from harnessix.models.contracts import (
    ReasoningDelta,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolArgumentsProgress,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.activity_indicator import activity_text
from harnessix.product_ui.generation_estimate import approximate, in_flight_request
from harnessix.product_ui.main_view import _status_text
from harnessix.product_ui.projection import (
    ProductViewState,
    TurnProjection,
    apply_events_next,
    apply_replay_page,
)
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import (
    EventsNextResult,
    EventsReplayResult,
    PublicEvent,
    PublicItemDelta,
    PublicUsage,
    UsagePublicEvent,
)
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.contracts.provider import model_request
from tests.models import anthropic_wire, wire
from tests.product_ui.test_cost_display import QWEN_FLASH
from tests.product_ui.test_tui_usability import NOW, _state, _thread

NAMES = {tool_alias("test.read"): "test.read"}
THREAD = _thread()


def test_adapters_report_argument_bytes_at_once_and_then_in_steps() -> None:
    progress = ArgumentProgress()
    # 第一段立即上报（界面马上知道模型开始生成参数了），按UTF-8字节计：三个汉字是9字节。
    assert progress.add("三个字", "apply_patch_batch") == [
        ToolArgumentsProgress(utf8_bytes=9, tool="apply_patch_batch")
    ]
    # 之后每满一段才再报；空片段不报。
    assert progress.add("x" * (PROGRESS_STEP_BYTES - 1), None) == []
    assert progress.add("", None) == []
    assert progress.add("x", None) == [ToolArgumentsProgress(utf8_bytes=9 + PROGRESS_STEP_BYTES)]


def test_chat_stream_reports_progress_while_tool_arguments_arrive() -> None:
    state = ChatStream(model_request(with_tools=True), NAMES, parallel=True, attempt_id=uuid4())
    body = "y" * 600
    events = []
    for value in (
        wire.chunk({"role": "assistant", "tool_calls": [wire.call(arguments='{"path": "')]}),
        wire.chunk({"tool_calls": [{"index": 0, "function": {"arguments": body}}]}),
        wire.chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}),
        wire.chunk(finish="tool_calls"),
        wire.chunk(usage=True),
    ):
        events.extend(state.feed(ChatCompletionChunk.model_validate(value, strict=True)))
    events.extend(state.finish(seen_done=True))

    reported = [event for event in events if isinstance(event, ToolArgumentsProgress)]
    assert [(event.utf8_bytes, event.tool) for event in reported] == [
        (10, "test.read"),
        (610, "test.read"),
    ]
    # 进度只是数字：工具调用本身照旧在整段到齐后给出，参数一字不差。
    (call,) = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert call.arguments == {"path": body}


def test_anthropic_stream_reports_progress_while_tool_input_arrives() -> None:
    state = AnthropicStream(
        model_request(with_tools=True), NAMES, parallel=True, attempt_id=uuid4()
    )
    events = []
    for frame in anthropic_wire.tool_frames(arguments='{"path": "中文.txt"}'):
        payload = frame.split(b"data: ", 1)[1]
        events.extend(state.feed(validate_event(json.loads(payload))))
    reported = [event for event in events if isinstance(event, ToolArgumentsProgress)]
    assert [(event.utf8_bytes, event.tool) for event in reported] == [
        (len('{"path": "中文.txt"}'.encode()), "test.read")
    ]


def _progress(clock) -> tuple[GenerationProgress, list[ItemDelta]]:
    published: list[ItemDelta] = []
    return GenerationProgress(model_request(), published.append, clock=clock), published


async def test_progress_is_published_at_most_once_a_second_and_only_when_it_changed() -> None:
    now = [100.0]
    progress, published = _progress(lambda: now[0])

    progress.reasoning("先想一下")  # 12字节，第一次立即发布
    progress.reasoning("abc")  # 同一秒内：只累计，到期再补发
    assert [delta.delta for delta in published] == ["12 0 0 -\n"]
    now[0] += 1.0
    progress.text("好")
    assert published[-1].delta == "15 3 0 -\n"
    now[0] += 5.0
    assert progress.observe(ToolArgumentsProgress(utf8_bytes=4096, tool="apply_patch_batch"))
    assert published[-1].delta == "15 3 4096 apply_patch_batch\n"
    # 其他事件不归它管。
    assert not progress.observe(ResponseCompleted())
    assert [delta.stream_sequence for delta in published] == [1, 2, 3]
    assert {delta.item_id for delta in published} == {
        generation_progress_item_id(progress.request.turn_id, progress.request.step)
    }


async def test_a_change_inside_the_throttle_window_is_published_when_the_window_ends() -> None:
    """实测百炼在补丁内容生成完之前不返回任何片段：参数开头的十个字节到达后可能几十秒没有新事件。
    这十个字节落在节流期内也必须发出去，否则界面要等到整段结束才知道模型在生成修改。"""
    published: list[ItemDelta] = []
    progress = GenerationProgress(model_request(), published.append, interval=0.05)

    progress.reasoning("想")
    assert progress.observe(ToolArgumentsProgress(utf8_bytes=10, tool="apply_patch_batch"))
    assert [delta.delta for delta in published] == ["3 0 0 -\n"]
    await asyncio.sleep(0.2)  # 之后没有任何新事件
    assert [delta.delta for delta in published] == ["3 0 0 -\n", "3 0 10 apply_patch_batch\n"]

    # 响应结束后不再补发。
    progress.text("好")
    progress.text("的")
    progress.close()
    await asyncio.sleep(0.2)
    assert [delta.delta for delta in published][2:] == ["3 3 10 apply_patch_batch\n"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "12 0 0 -\n15 3 4096 apply_patch_batch\n",
            GenerationCounts(15, 3, 4096, "apply_patch_batch"),
        ),
        ("7 0 0 -\n", GenerationCounts(7, 0, 0, None)),
        ("", None),
        ("不是进度\n", None),
        ("1 2 x tool\n", None),
    ],
)
def test_only_the_latest_well_formed_line_counts(text: str, expected) -> None:
    assert latest_counts(text) == expected


async def test_runtime_publishes_progress_without_touching_items_or_the_tool_call(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="r1"),
                ReasoningDelta(delta="先想一下怎么改"),
                ToolArgumentsProgress(utf8_bytes=2048, tool="test.read"),
                ToolCallCompleted(call_id="c1", tool="test.read", arguments={"path": "a.txt"}),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            [
                ResponseStarted(response_id="r2"),
                TextStarted(content_id="t"),
                TextDelta(content_id="t", delta="完成"),
                TextCompleted(content_id="t", text="完成"),
                ResponseCompleted(),
            ],
        ]
    )
    deltas: list[ItemDelta] = []
    async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), provider) as runtime:
        runtime.subscribe_deltas(deltas.append)
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "改一下", request_id="r1")

    first = generation_progress_item_id(turn.turn_id, 1)
    lines = [delta.delta for delta in deltas if delta.item_id == first]
    # 思考先发布一次；工具参数的进度在同一秒内到达，响应随即结束，待补发的一行被取消。
    assert lines == [f"{len('先想一下怎么改'.encode())} 0 0 -\n"]
    # 思考预览照旧发布，并在参数开始生成时收尾。
    assert any(delta.item_id == reasoning_item_id(turn.turn_id, 1) for delta in deltas)
    # 进度不建Item，也不影响这一步的工具调用被照常处理、下一步照常进行。
    assert all(item.item_id != first for item in turn.items)
    assert turn.status.value == "completed" and len(provider.requests) == 2
    # 第二步是正文：正文的字节数同样计入进度。
    second = [d.delta for d in deltas if d.item_id == generation_progress_item_id(turn.turn_id, 2)]
    assert second == ["0 6 0 -\n"]


def _delta(turn_id, step: int, sequence: int, text: str, item_id=None) -> PublicItemDelta:
    return PublicItemDelta(
        thread_id=THREAD.thread_id,
        turn_id=turn_id,
        item_id=item_id or generation_progress_item_id(turn_id, step),
        model_step=step,
        stream_sequence=sequence,
        delta=text,
    )


def _page(*deltas: PublicItemDelta, cursor: int = THREAD.cursor) -> EventsNextResult:
    replay = EventsReplayResult(
        thread_id=THREAD.thread_id, events=(), has_more=False, scanned_through=cursor
    )
    return EventsNextResult(replay=replay, deltas=deltas)


def test_each_preview_stream_reaches_only_clients_that_declared_it() -> None:
    turn_id, text_item = uuid4(), uuid4()
    page = _page(
        _delta(turn_id, 1, 1, "想", item_id=reasoning_item_id(turn_id, 1)),
        _delta(turn_id, 1, 1, "3 0 0 -\n"),
        _delta(turn_id, 1, 1, "答", item_id=text_item),
    )

    def shown(previews: PreviewStreams) -> list[str]:
        return [delta.delta for delta in previews.visible(page).deltas]

    # 什么都没声明的客户端（exec、旧版SDK）只收到正文：进度行不会被当成正文打印出来。
    assert shown(NO_PREVIEWS) == ["答"]
    assert shown(PreviewStreams(reasoning=True)) == ["想", "答"]
    assert shown(PreviewStreams(progress=True)) == ["3 0 0 -\n", "答"]
    assert PreviewStreams(reasoning=True, progress=True).visible(page) is page


@pytest.mark.parametrize("declared", [True, False])
async def test_over_the_protocol_progress_goes_only_to_a_client_that_asked_for_it(
    tmp_path: Path, declared: bool
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="r"),
                ReasoningDelta(delta="想一想"),
                TextStarted(content_id="t"),
                TextDelta(content_id="t", delta="流式文本"),
                TextCompleted(content_id="t", text="流式文本"),
                ResponseCompleted(),
            ]
        ],
        delay_seconds=0.02,
    )
    async with AgentRuntime(store, provider) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        # 终端界面声明generation_progress；exec与其他SDK客户端用默认值（不声明）。
        client = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(service)), generation_progress=declared
        )
        initialized = await client.initialize()
        assert initialized.capabilities.generation_progress is declared
        thread = await client.create_thread(str(tmp_path), request_id="create")
        cursor = thread.cursor
        started = await client.start_turn(thread.thread_id, "输出文本", request_id="turn")
        progress = generation_progress_item_id(started.turn_id, 1)
        seen: list[PublicItemDelta] = []
        for _ in range(200):
            page = await client.next_events(
                thread.thread_id, after_cursor=cursor, wait_ms=100, limit=100
            )
            cursor = max(cursor, page.replay.scanned_through)
            seen.extend(page.deltas)
            if any(
                event.data.type == "turn_state_changed" and event.data.status == "completed"
                for event in page.replay.events
            ):
                break
        await client.close()

    assert "流式文本" in [delta.delta for delta in seen]
    lines = [delta.delta for delta in seen if delta.item_id == progress]
    # 不声明的客户端一行进度也收不到：否则它会把“9 0 0 -”当成助手正文显示。
    assert (lines != []) is declared
    assert all(latest_counts(line) is not None for line in lines)


def _calling(model_step: int = 0, usage: PublicUsage | None = None) -> ProductViewState:
    turn = TurnProjection(
        turn_id=uuid4(),
        request_id="r",
        status="calling_model",
        budget=None,
        usage=usage,
        model_step=model_step,
        usage_cursor=THREAD.cursor,
    )
    return ProductViewState(thread=THREAD, durable_cursor=THREAD.cursor, current_turn=turn)


def test_the_tui_names_the_phase_and_estimates_what_was_generated() -> None:
    state = _calling()
    turn_id = state.current_turn.turn_id
    assert in_flight_request(state) is None

    thinking = apply_events_next(state, _page(_delta(turn_id, 1, 1, "3600 0 0 -\n")))
    request = in_flight_request(thinking)
    assert (request.phase, request.output_tokens) == ("thinking", 1200)
    assert activity_text("calling_model", 23, 0, streaming=False, generation=request) == (
        "⠋ 思考中 · 约 1,200 tokens · 已运行 23 秒"
    )
    # 进度有一阵没有变化（服务商整段返回补丁内容，或网络停顿）：说明等了多久，
    # 不停在一个不动的数字上。
    waiting = activity_text(
        "calling_model", 60, 0, streaming=False, generation=request, silent_seconds=18.4
    )
    assert waiting == "⠋ 思考中 · 模型服务 18 秒未返回新内容 · 已运行 1 分 00 秒"
    assert "约 1,200 tokens" in activity_text(
        "calling_model", 60, 0, streaming=False, generation=request, silent_seconds=4.9
    )

    # 序号有缺口也不要紧：每行都是累计值，只看最近到达的一行。
    editing = apply_events_next(
        thinking, _page(_delta(turn_id, 1, 5, "3600 0 6600 apply_patch_batch\n"))
    )
    request = in_flight_request(editing)
    assert (request.phase, request.output_tokens) == ("editing", 3400)
    assert request.label == "正在生成修改内容 · 约 3,400 tokens"

    other = apply_events_next(editing, _page(_delta(turn_id, 1, 6, "0 0 900 exec_command.x\n")))
    assert in_flight_request(other).phase_label == "正在生成操作内容"
    replying = apply_events_next(other, _page(_delta(turn_id, 1, 7, "0 300 0 -\n")))
    assert in_flight_request(replying).label == "生成回复中 · 约 100 tokens"

    # 进度流只驱动状态显示，不进入对话区。
    assert transcript_lines(editing) == ()

    # 同一步骤的请求重试后，运行时的序号从1重新开始：界面显示新的数字，不停在上一次尝试上。
    retried = apply_events_next(editing, _page(_delta(turn_id, 1, 1, "300 0 0 -\n")))
    assert in_flight_request(retried).label == "思考中 · 约 100 tokens"


def test_progress_is_dropped_once_the_step_is_settled() -> None:
    state = _calling()
    turn = state.current_turn
    running = apply_events_next(state, _page(_delta(turn.turn_id, 1, 1, "0 0 900 -\n")))
    assert in_flight_request(running) is not None
    settled = replace(running, current_turn=replace(turn, status="executing_tools", model_step=1))
    cleared = apply_events_next(settled, _page())
    assert cleared.streams == () and in_flight_request(cleared) is None


def _settle_step(view: ProductViewState, step: int, usage: PublicUsage) -> ProductViewState:
    event = PublicEvent(
        event_id=uuid4(),
        thread_id=THREAD.thread_id,
        turn_id=view.current_turn.turn_id,
        cursor=view.durable_cursor + 1,
        occurred_at=NOW,
        data=UsagePublicEvent(step=step, usage=usage),
    )
    page = EventsReplayResult(
        thread_id=THREAD.thread_id, events=(event,), scanned_through=event.cursor, has_more=False
    )
    return apply_replay_page(view, page)


def test_the_status_bar_estimates_tokens_and_cost_while_a_request_is_running() -> None:
    def status(view: ProductViewState) -> str:
        return _status_text(replace(_state(view, 1), list_price=QWEN_FLASH))

    # 第1步已结算：30,000输入＋1,000输出。
    first = PublicUsage(input_tokens=30_000, output_tokens=1_000, total_tokens=31_000)
    view = _settle_step(_calling(), 1, first)
    assert " · 本轮 31,000 tokens · 费用≤¥0.0267" in status(view)

    # 第2步进行中，已生成约2,000 tokens：输入按上一步的31,000估算，标明“约”和“≈”。
    turn_id = view.current_turn.turn_id
    progress = _delta(turn_id, 2, 1, "0 0 6000 apply_patch_batch\n")
    running = apply_events_next(view, _page(progress, cursor=view.durable_cursor))
    text = status(running)
    assert text.startswith("正在生成修改内容 · ")
    assert " · 本轮 约 64,000 tokens · 费用≈¥0.0569" in text

    # 这一步结算后换回服务商报回的实际用量。
    second = PublicUsage(input_tokens=32_500, output_tokens=2_300, total_tokens=34_800)
    done = _settle_step(running, 2, second)
    settled = apply_events_next(done, _page(cursor=done.durable_cursor))
    assert " · 本轮 65,800 tokens · 费用≤¥0.0589" in status(settled)


@pytest.mark.parametrize(
    ("tokens", "shown"), [(0, 10), (4, 10), (874, 870), (3_449, 3_400), (38_412, 38_000)]
)
def test_estimates_keep_two_or_three_significant_digits(tokens: int, shown: int) -> None:
    assert approximate(tokens) == shown
