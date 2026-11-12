"""模型思考内容的实时预览（0.10.0b9）：适配器转发、运行时独立序号发布、协商过滤、界面显示与清除。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from openai.types.chat import ChatCompletionChunk

from harnessix.agent.generation_progress import generation_progress_item_id
from harnessix.agent.models import ItemDelta, TextContent
from harnessix.agent.reasoning_preview import MAX_REASONING_PREVIEW_BYTES, reasoning_item_id
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.preview_streams import NO_PREVIEWS
from harnessix.models._chat_stream import ChatStream
from harnessix.models.contracts import (
    ReasoningDelta,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.projection import (
    ProductViewState,
    TurnProjection,
    apply_events_next,
    apply_item_delta,
)
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import (
    EventsNextResult,
    EventsReplayResult,
    PublicItemDelta,
)
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.test_publication import protected
from tests.contracts.provider import model_request
from tests.models import wire
from tests.product_ui.test_tui_usability import _thread


def test_chat_stream_forwards_reasoning_without_counting_it_as_text() -> None:
    state = ChatStream(model_request(), {}, parallel=True, attempt_id=uuid4())
    events = []
    for value in (
        wire.chunk({"role": "assistant", "reasoning_content": "先想一下"}),
        wire.chunk({"content": "好"}),
        wire.chunk(finish="stop"),
        wire.chunk(usage=True),
    ):
        events.extend(state.feed(ChatCompletionChunk.model_validate(value, strict=True)))
    events.extend(state.finish(seen_done=True))
    assert [e.delta for e in events if isinstance(e, ReasoningDelta)] == ["先想一下"]
    assert [e.delta for e in events if isinstance(e, TextDelta)] == ["好"]
    completed = next(e for e in events if isinstance(e, TextCompleted))
    assert completed.text == "好"


async def test_runtime_publishes_reasoning_on_its_own_sequence(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="r"),
                ReasoningDelta(delta="想"),
                TextStarted(content_id="t"),
                TextDelta(content_id="t", delta="答"),
                ReasoningDelta(delta="再想"),
                TextDelta(content_id="t", delta="案"),
                TextCompleted(content_id="t", text="答案"),
                ResponseCompleted(),
            ]
        ]
    )
    deltas: list[ItemDelta] = []
    async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), provider) as runtime:
        runtime.subscribe_deltas(deltas.append)
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "你好", request_id="r1")

    preview = reasoning_item_id(turn.turn_id, 1)
    reasoning = [d for d in deltas if d.item_id == preview]
    # 生成进度是另一条预览流（只有数字），与正文、思考各用各的序号。
    progress = generation_progress_item_id(turn.turn_id, 1)
    text = [d for d in deltas if d.item_id not in (preview, progress)]
    # 正文开始即视为思考收尾；之后再出现的思考不再预览，不影响模型步骤。
    assert [(d.stream_sequence, d.delta) for d in reasoning] == [(1, "想")]
    # 正文序号不被思考预览占用，界面不会误判正文缺口。
    assert [d.stream_sequence for d in text] == list(range(1, len(text) + 1))
    assert "".join(d.delta for d in text) == "答案"
    # 思考只作预览：不建Item，模型历史与持久正文都没有它。
    assert all(item.item_id != preview for item in turn.items)
    assert [i.content.text for i in turn.items if isinstance(i.content, TextContent)][-1] == "答案"


async def test_short_reasoning_is_released_when_it_ends_under_secret_protection(
    tmp_path: Path,
) -> None:
    # 2026-11-10 macOS实测：公开保护扣住末尾可能构成密钥的字节，短思考此前一个字也没显示。
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="r"),
                ReasoningDelta(delta="用户问1+1"),
                ReasoningDelta(delta="，答2。"),
                TextStarted(content_id="t"),
                TextCompleted(content_id="t", text="2"),
                ResponseCompleted(),
            ]
        ]
    )
    deltas: list[ItemDelta] = []
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, public_output_protection=scope
        ) as runtime:
            runtime.subscribe_deltas(deltas.append)
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "1+1", request_id="r1")
    preview = [d.delta for d in deltas if d.item_id == reasoning_item_id(turn.turn_id, 1)]
    assert "".join(preview) == "用户问1+1，答2。"


async def test_reasoning_preview_stops_at_its_size_limit(tmp_path: Path) -> None:
    big = "想" * (MAX_REASONING_PREVIEW_BYTES // 3)
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="r"),
                ReasoningDelta(delta=big),
                ReasoningDelta(delta="超出上限后不再预览"),
                TextStarted(content_id="t"),
                TextCompleted(content_id="t", text="好"),
                ResponseCompleted(),
            ]
        ]
    )
    deltas: list[ItemDelta] = []
    async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), provider) as runtime:
        runtime.subscribe_deltas(deltas.append)
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "你好", request_id="r1")
    preview = [d.delta for d in deltas if d.item_id == reasoning_item_id(turn.turn_id, 1)]
    assert "".join(preview) == big
    assert turn.status.value == "completed"


def _delta(turn_id, step: int, sequence: int, text: str, item_id=None) -> PublicItemDelta:
    return PublicItemDelta(
        thread_id=THREAD.thread_id,
        turn_id=turn_id,
        item_id=item_id or reasoning_item_id(turn_id, step),
        model_step=step,
        stream_sequence=sequence,
        delta=text,
    )


THREAD = _thread()


def _page(*deltas: PublicItemDelta) -> EventsNextResult:
    replay = EventsReplayResult(
        thread_id=THREAD.thread_id,
        events=(),
        has_more=False,
        scanned_through=THREAD.cursor,
    )
    return EventsNextResult(replay=replay, deltas=deltas)


def test_clients_without_the_capability_get_text_deltas_only() -> None:
    turn_id, text_item = uuid4(), uuid4()
    page = _page(_delta(turn_id, 1, 1, "想"), _delta(turn_id, 1, 1, "答", item_id=text_item))
    assert [d.delta for d in NO_PREVIEWS.visible(page).deltas] == ["答"]


def test_the_tui_shows_recent_reasoning_and_drops_it_after_the_step() -> None:
    turn = TurnProjection(
        turn_id=uuid4(),
        request_id="r",
        status="calling_model",
        budget=None,
        usage=None,
        model_step=0,
    )
    state = ProductViewState(thread=THREAD, durable_cursor=THREAD.cursor, current_turn=turn)
    lines = "第一行\n第二行\n第三行\n第四行"
    state = apply_events_next(state, _page(_delta(turn.turn_id, 1, 1, lines)))
    shown = transcript_lines(state)
    assert [(line.role, line.text, line.final) for line in shown] == [
        ("思考", "第二行 第三行 第四行", False)
    ]
    # 步骤用量结算或轮次结束后清除，不会一直挂在实时区。
    settled = ProductViewState(
        thread=THREAD,
        durable_cursor=THREAD.cursor,
        current_turn=TurnProjection(
            turn_id=turn.turn_id,
            request_id="r",
            status="executing_tools",
            budget=None,
            usage=None,
            model_step=1,
        ),
        streams=state.streams,
    )
    assert transcript_lines(apply_events_next(settled, _page())) == ()
    # 正文流照旧显示为“生成中”。
    text = apply_item_delta(state, _delta(turn.turn_id, 1, 1, "答", item_id=uuid4()))
    assert ("Harnessix（生成中）", "答") in [
        (line.role, line.text) for line in transcript_lines(text)
    ]
