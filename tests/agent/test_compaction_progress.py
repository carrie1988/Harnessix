"""压缩较早的对话时，界面写明在做什么（ADR 0167）。

此前压缩期间Turn处于准备上下文，运行提示与底栏只有“准备中”，一次压缩要等几十秒到几分钟。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from harnessix.agent.generation_progress import (
    compaction_progress_item_id,
    generation_progress_item_id,
    latest_counts,
)
from harnessix.agent.models import ItemDelta
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.preview_streams import PreviewStreams
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.activity_indicator import activity_text
from harnessix.product_ui.generation_estimate import compacting, in_flight_request
from harnessix.product_ui.projection import ProductViewState, TurnProjection, apply_events_next
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.agent.test_generation_progress import THREAD, _delta, _page
from tests.context.test_compaction_runtime import SequenceProvider, accounted_text, config


async def test_the_runtime_publishes_compaction_progress_under_its_own_identity(
    tmp_path: Path,
) -> None:
    summary = ScriptedProvider([accounted_text("保留public接口；解析器失败恢复仍需验证。")])
    normal = SequenceProvider(
        [accounted_text("解析器调查事实与失败恢复约束。\n" * 600), accounted_text("已完成。")]
    )
    deltas: list[ItemDelta] = []
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    async with AgentRuntime(
        store, normal, RecordingTools(), compaction=config(), summary_provider=summary
    ) as runtime:
        runtime.subscribe_deltas(deltas.append)
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "调查解析器", request_id="seed")
        turn = await runtime.run_turn(thread.thread_id, "继续修复", request_id="continue")

    assert [record.status for record in turn.compactions] == ["summarized"]
    step = turn.compactions[0].plan.model_step
    identity = compaction_progress_item_id(turn.turn_id, step)
    lines = [delta.delta for delta in deltas if delta.item_id == identity]
    # 摘要请求开始前一行（界面马上知道在压缩）；与该步主请求的进度流不同身份。
    assert lines == ["0 0 0 -\n"] and latest_counts(lines[0]) is not None
    assert identity != generation_progress_item_id(turn.turn_id, step)


def test_clients_that_did_not_declare_progress_do_not_receive_it() -> None:
    turn_id = uuid4()
    page = _page(
        _delta(turn_id, 2, 1, "0 0 0 -\n", item_id=compaction_progress_item_id(turn_id, 2))
    )
    assert PreviewStreams().visible(page).deltas == ()
    assert PreviewStreams(progress=True).visible(page) is page


def _preparing(status: str = "preparing_context") -> ProductViewState:
    turn = TurnProjection(
        turn_id=uuid4(), request_id="r", status=status, budget=None, usage=None, model_step=1
    )
    return ProductViewState(thread=THREAD, durable_cursor=THREAD.cursor, current_turn=turn)


def test_the_tui_says_it_is_compacting_and_how_long_it_took() -> None:
    state = _preparing()
    turn_id = state.current_turn.turn_id
    assert not compacting(state)
    progress = _delta(turn_id, 2, 1, "0 0 0 -\n", item_id=compaction_progress_item_id(turn_id, 2))
    view = apply_events_next(state, _page(progress))
    assert compacting(view)
    # 压缩进度不是这一步主请求的生成进度：不给出“思考中/生成回复中”的估算。
    assert in_flight_request(view) is None
    assert activity_text(
        "preparing_context", 75, 0, streaming=False, phase_seconds=40.2, compaction=True
    ) == ("⠋ 正在压缩较早的对话 · 已用时 40 秒 · 已运行 1 分 15 秒")
    # 没有压缩时，准备上下文仍是“准备中”。
    assert activity_text("preparing_context", 3, 0, streaming=False) == "⠋ 准备中 · 已运行 3 秒"

    # 开始调用模型后，压缩进度清除，界面回到主请求的显示。
    calling = replace(view, current_turn=replace(view.current_turn, status="calling_model"))
    calling = apply_events_next(calling, _page())
    assert not compacting(calling) and not any(stream.compaction for stream in calling.streams)
