"""写入方预填解析缓存（ADR 0168 D1）：解析写入的原字节等于写入方的Thread；不等时不交出写入方的对象。

往返性质另有两层证据：根conftest让整个测试集的每次预填都先解析提交的原字节并逐层比较，不同即判失败；
这里再在评测同款装配的产品运行上逐次核对。
"""

from __future__ import annotations

import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.models import (
    Budget,
    EffectClass,
    EventDraft,
    ItemStarted,
    TextContent,
    Thread,
    ThreadCreated,
    ToolCallContent,
    TurnStarted,
    TurnStatus,
)
from harnessix.session import parsed_projection_cache
from harnessix.session.parsed_projection_cache import ParsedProjectionCache
from harnessix.session.projection_identity import identical
from harnessix.session.sqlite import SQLiteSessionStore
from tests.session.test_authenticated_history import binding, seed

_AT = datetime(2026, 11, 18, tzinfo=UTC)


def _thread(**updates: object) -> Thread:
    return Thread(thread_id=uuid4(), workspace="/w", created_at=_AT, updated_at=_AT, **updates)


def _call(arguments: dict[str, object]) -> ToolCallContent:
    return ToolCallContent(
        call_id=uuid4(),
        provider_call_id="call-1",
        tool="read_file",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
        arguments=arguments,
    )


def test_identical_holds_for_a_parse_of_the_serialized_thread():
    # Reducer的输入（事件与上一份投影）都来自JSON解析，时间带解析器的时区对象。
    thread = Thread.model_validate_json(_thread(sequence=3).model_dump_json())
    assert identical(Thread.model_validate_json(thread.model_dump_json()), thread)
    assert identical(float("nan"), float("nan"))


@pytest.mark.parametrize(
    "left,right",
    [
        (TurnStatus.COMPLETED, "completed"),
        (True, 1),
        (0.0, -0.0),
        ((1, 2), [1, 2]),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
        (_call({"a": 1, "b": 2}), _call({"b": 2, "a": 1})),
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            Thread.model_validate_json(_thread().model_dump_json()).created_at.replace(
                year=2026, month=1, day=1
            ),
        ),
    ],
    ids=["str_enum", "bool_int", "negative_zero", "tuple_list", "key_order", "model", "tz"],
)
def test_identical_is_stricter_than_equality(left, right):
    """这些成对的值用==比较相等（或只差字段顺序），重新序列化或后续处理时却可能不同。"""
    if not isinstance(left, ToolCallContent) and not isinstance(left, tuple):
        assert left == right or math.copysign(1.0, left) != math.copysign(1.0, right)
    assert not identical(left, right)


@pytest.mark.projection_prefill_mismatch
async def test_a_prefill_that_differs_from_the_committed_bytes_is_never_served(
    tmp_path, monkeypatch, caplog
):
    """抽样核对发现解析结果与写入方的Thread不同：不预填，读取方拿到的是提交原字节的解析结果；
    记一条诊断，此后这个缓存不再预填。"""
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        store._parsed_projections = cache = ParsedProjectionCache(check_every=1)
        dump = Thread.model_dump_json

        def moved(self, *args, **kwargs):
            return dump(self.model_copy(update={"workspace": "/else"}), *args, **kwargs)

        turn_id = uuid4()
        start = TurnStarted(request_id="fixture", request_fingerprint="1" * 64, budget=Budget())
        with monkeypatch.context() as patch:
            patch.setattr(Thread, "model_dump_json", moved)
            with caplog.at_level(logging.WARNING, logger=parsed_projection_cache.__name__):
                written = await store.append(
                    tid, [EventDraft(turn_id=turn_id, payload=start)], expected_sequence=1
                )

        read = await store.get_thread(tid)
        assert written.workspace != "/else" and read.workspace == "/else"
        assert read is not written
        assert cache.prefill_mismatches == 1 and "prefill disabled" in caplog.text

        message = TextContent(kind="user_message", text="next")
        item = EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=uuid4(), content=message))
        appended = await store.append(tid, [item], expected_sequence=2)
        again = await store.get_thread(tid)
        assert again == appended and again is not appended
        assert (cache.prefills, cache.prefill_checks) == (1, 1)


async def test_an_append_that_rolls_back_is_never_prefilled(tmp_path):
    armed = False

    def fault(point: str) -> None:
        if armed and point == "session.after_projection":
            raise OSError("故障夹具：投影写入后回滚")

    store = SQLiteSessionStore(tmp_path / "state.db", fault=fault)
    await store.initialize()
    tid = uuid4()
    created = await store.append(
        tid, [EventDraft(payload=ThreadCreated(workspace="/w"))], expected_sequence=0
    )
    start = TurnStarted(request_id="fixture", request_fingerprint="1" * 64, budget=Budget())
    drafts = [EventDraft(turn_id=uuid4(), payload=start)]
    armed = True
    with pytest.raises(Exception, match="回滚|文件系统|storage|存储"):
        await store.append(tid, drafts, expected_sequence=1)
    armed = False

    assert await store.get_thread(tid) is created
    appended = await store.append(tid, drafts, expected_sequence=1)
    assert await store.get_thread(tid) is appended


@pytest.mark.skipif(os.name != "posix", reason="外部边界命令Profile只在POSIX上可用")
async def test_every_projection_of_a_product_run_round_trips_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """评测同款装配（审批、整组补丁、命令、Artifact、模型尝试与用量）跑九个工具步骤，每次预填都解析
    提交的原字节并逐层比较。"""
    from scripts.host_step_overhead_bench import format_report, run_benchmark

    checked: list[tuple[bool, int]] = []
    original = parsed_projection_cache._round_trips

    def recorded(thread, written):  # type: ignore[no-untyped-def]
        checked.append((original(thread, written), thread.sequence))
        return checked[-1][0]

    monkeypatch.setattr(parsed_projection_cache, "_round_trips", recorded)

    report = await run_benchmark(tmp_path, steps=9)

    assert report.valid, format_report(report)
    mismatched = [sequence for same, sequence in checked if not same]
    assert len(checked) > 9 * 8 and mismatched == [], (len(checked), mismatched)
