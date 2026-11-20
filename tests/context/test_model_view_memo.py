"""模型视图的逐项结果复用（ADR 0155）：复用与逐项重算逐字节一致，内容变化必定重新核对。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Item,
    ItemStatus,
    TextContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.context import tool_result_view
from harnessix.context.model_view_memo import ModelViewMemo, content_fingerprint, memo_key
from harnessix.context.tool_result_contracts import ToolResultViewDecision, ToolResultViewPolicy
from harnessix.context.tool_result_view import (
    PreparedModelHistory,
    history_items,
    prepare_model_history,
    view_history_sha256,
)
from harnessix.domain.models import EffectClass, utc_now

POLICY = ToolResultViewPolicy(max_inline_utf8_bytes=2048)


def done(content) -> Item:
    return Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)


def exchange(output, *, archived: bool = False) -> tuple[Item, Item]:
    """一次工具调用及其结果；archived的结果带完整Artifact，超过上限时走引用策略。"""
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="call",
        tool="test.read",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
        arguments={"path": "源码/模块.py", "limit": 40},
    )
    if archived:
        body = (json.dumps(output, ensure_ascii=False) + "\n").encode()
        reference = ArtifactRef(
            artifact_id=uuid4(),
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            records=1,
            complete=True,
            expires_at=utc_now() + timedelta(hours=1),
        )
        output = {"preview": output, "artifact": reference.model_dump(mode="json")}
    result = ToolResultContent(call_id=call.call_id, outcome="succeeded", output=output)
    return done(call), done(result)


OUTPUTS: tuple[tuple[object, bool], ...] = (
    ({"text": "第一段🙂\n" * 20, "revision": 1, "ratio": 1.0, "ok": True}, False),
    ({"数据": ["needle🙂" * 600]}, True),
    ([1, 1.5, None, "é", {"嵌套": []}], False),
    ("plain text result", False),
    ({"matches": [{"path": f"a/{index}.py", "line": index} for index in range(30)]}, False),
)


def thread_of(
    items: tuple[Item, ...], decisions: tuple[ToolResultViewDecision, ...] = ()
) -> Thread:
    turn = Turn(
        turn_id=uuid4(),
        request_id="request",
        request_fingerprint="0" * 64,
        budget=Budget(),
        created_at=utc_now(),
        items=items,
        status=TurnStatus.PREPARING_CONTEXT,
        tool_result_view_decisions=decisions,
    )
    return Thread(
        thread_id=uuid4(),
        workspace="/workspace",
        turns=(turn,),
        sequence=1,
        active_turn_id=turn.turn_id,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def reread(thread: Thread) -> Thread:
    """与Store读取一样：同一份内容，全新的对象。"""
    return Thread.model_validate_json(thread.model_dump_json())


def serialized(prepared: PreparedModelHistory) -> str:
    return json.dumps(
        [
            [item.model_dump_json() for item in prepared.history],
            prepared.inspection.model_dump_json(),
            [decision.model_dump_json() for decision in prepared.new_decisions],
            [
                [str(ref.call_id), ref.binding.model_dump_json(), ref.omitted_field]
                for ref in prepared.references
            ],
        ]
    )


@pytest.fixture
def memo(monkeypatch: pytest.MonkeyPatch) -> ModelViewMemo:
    """每个用例一张空表，不受其他用例留下的记录影响。"""
    fresh = ModelViewMemo()
    monkeypatch.setattr(tool_result_view, "_MEMO", fresh)
    return fresh


def from_scratch(monkeypatch: pytest.MonkeyPatch, thread: Thread, **kwargs) -> PreparedModelHistory:
    """用不保存任何结果的表重算一遍，再换回原表。"""
    with monkeypatch.context() as patch:
        patch.setattr(tool_result_view, "_MEMO", ModelViewMemo(max_entries=0))
        return prepare_model_history(thread, 1, POLICY, **kwargs)


def test_fingerprint_is_equal_for_reread_content_and_strict_about_json_types() -> None:
    _, result = exchange({"count": 1, "note": None, "path": "a.py"})

    def variant(output) -> Item:
        content = result.content.model_copy(update={"output": output})
        return result.model_copy(update={"content": content})

    base = content_fingerprint(result)
    assert base is not None
    assert content_fingerprint(Item.model_validate_json(result.model_dump_json())) == base
    for output in (
        {"count": 1.0, "note": None, "path": "a.py"},
        {"count": True, "note": None, "path": "a.py"},
        {"note": None, "count": 1, "path": "a.py"},
        {"count": 1, "note": "", "path": "a.py"},
    ):
        assert content_fingerprint(variant(output)) not in {None, base}
    # JSON模式会把非有限数字写成null；这样的内容没有指纹，只能逐项重算。
    for value in (float("nan"), float("inf")):
        assert content_fingerprint(variant({"count": 1, "note": value, "path": "a.py"})) is None
    assert content_fingerprint(result.model_copy(update={"item_id": uuid4()})) != base


def test_memo_reuses_only_the_same_key_skips_missing_fingerprints_and_evicts_oldest() -> None:
    table = ModelViewMemo(max_entries=2)
    calls: list[str] = []

    def compute(name: str):
        return lambda: calls.append(name) or name

    first, second, third = (memo_key("kind", bytes([index])) for index in range(3))
    assert memo_key("kind", b"a", None) is None
    assert table.recall(first, compute("first")) == "first"
    assert table.recall(first, compute("again")) == "first"
    assert table.recall(None, compute("no-key")) == "no-key"
    assert table.recall(second, compute("second")) == "second"
    assert table.recall(first, compute("again")) == "first"
    assert table.recall(third, compute("third")) == "third"  # 淘汰最久未用的second
    assert table.recall(second, compute("second-again")) == "second-again"
    assert calls == ["first", "no-key", "second", "third", "second-again"]
    assert len(table) == 2


def test_failed_computation_is_not_recorded() -> None:
    table = ModelViewMemo()
    key = memo_key("kind", b"a")

    def fail() -> str:
        raise KernelError("context_tool_result_decision_mismatch", "不一致")

    with pytest.raises(KernelError):
        table.recall(key, fail)
    assert len(table) == 0 and table.recall(key, lambda: "value") == "value"


def test_reused_view_is_byte_identical_to_from_scratch_at_every_step(
    memo: ModelViewMemo, monkeypatch: pytest.MonkeyPatch
) -> None:
    items: tuple[Item, ...] = (done(TextContent(kind="user_message", text="检查对账")),)
    decisions: tuple[ToolResultViewDecision, ...] = ()
    for output, archived in OUTPUTS:
        items = (*items, done(TextContent(kind="assistant_message", text="继续")))
        items = (*items, *exchange(output, archived=archived))
        thread = thread_of(items, decisions)

        reused = prepare_model_history(reread(thread), 1, POLICY)
        expected = from_scratch(monkeypatch, reread(thread))
        assert reused == expected and serialized(reused) == serialized(expected)
        # Reducer入口：随事件提交的新决定；以及读完即弃的不拷贝视图。
        for kwargs in (
            {"decisions": reused.new_decisions},
            {"detached": False},
            {"decisions": reused.new_decisions, "detached": False},
        ):
            again = prepare_model_history(reread(thread), 1, POLICY, **kwargs)
            assert again == expected and serialized(again) == serialized(expected)
            assert again == from_scratch(monkeypatch, reread(thread), **kwargs)
        source = history_items(thread)
        assert (
            reused.inspection.source_history_sha256
            == hashlib.sha256(
                json.dumps(
                    [item.model_dump(mode="json") for item in source],
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        assert reused.inspection.view_history_sha256 == view_history_sha256(reused.history)
        decisions = (*decisions, *reused.new_decisions)
    assert [decision.strategy for decision in decisions].count("artifact_reference") == 1
    assert len(memo) > 0


def test_unchanged_items_are_verified_once_and_any_content_change_is_verified_again(
    memo: ModelViewMemo, monkeypatch: pytest.MonkeyPatch
) -> None:
    items: tuple[Item, ...] = (done(TextContent(kind="user_message", text="检查")),)
    for output, archived in OUTPUTS[:3]:
        items = (*items, *exchange(output, archived=archived))
    first = prepare_model_history(thread_of(items), 1, POLICY)
    frozen = thread_of(items, first.new_decisions)
    verified: list[object] = []
    original = tool_result_view._apply_decision

    def counting(item, call, decision, policy):
        verified.append(item.item_id)
        return original(item, call, decision, policy)

    monkeypatch.setattr(tool_result_view, "_apply_decision", counting)
    prepare_model_history(reread(frozen), 2, POLICY)
    assert len(verified) == 3
    prepare_model_history(reread(frozen), 3, POLICY)
    prepare_model_history(reread(frozen), 3, POLICY, detached=False)
    assert len(verified) == 3

    # 同一Item身份，输出里的1.0改写成1：Python相等比较分不出，指纹与逐项重算都必须分得出。
    result = items[2]
    assert isinstance(result.content, ToolResultContent) and isinstance(result.content.output, dict)
    changed_output = {**result.content.output, "ratio": 1}
    assert changed_output == result.content.output
    changed = result.model_copy(
        update={"content": result.content.model_copy(update={"output": changed_output})}
    )
    tampered = thread_of((*items[:2], changed, *items[3:]), first.new_decisions)
    with pytest.raises(KernelError) as reused_error:
        prepare_model_history(reread(tampered), 3, POLICY)
    with pytest.raises(KernelError) as scratch_error:
        from_scratch(monkeypatch, reread(tampered))
    assert reused_error.value.code == scratch_error.value.code
    assert reused_error.value.code == "context_tool_result_decision_mismatch"
    assert len(verified) == 5


def test_detached_views_stay_independent_copies_and_undetached_views_are_the_session_items(
    memo: ModelViewMemo,
) -> None:
    items: tuple[Item, ...] = (done(TextContent(kind="user_message", text="检查")),)
    for output, archived in OUTPUTS[:2]:
        items = (*items, *exchange(output, archived=archived))
    first = prepare_model_history(thread_of(items), 1, POLICY)
    frozen = thread_of(items, first.new_decisions)
    before = frozen.model_dump_json()

    warm = prepare_model_history(frozen, 2, POLICY)
    assert all(view is not source for view, source in zip(warm.history, items, strict=True))
    inline_output = warm.history[2].content.output
    inline_output["text"] = "mutated"
    warm.history[1].content.arguments["path"] = "mutated"
    warm.history[4].content.output["model_view"]["reason"] = "mutated"

    again = prepare_model_history(frozen, 2, POLICY)
    assert again.history[2].content.output["text"] == "第一段🙂\n" * 20
    assert again.history[1].content.arguments["path"] == "源码/模块.py"
    assert again.history[4].content.output["model_view"]["reason"] == "inline_budget_exceeded"
    assert again.inspection == warm.inspection and frozen.model_dump_json() == before

    shared = prepare_model_history(frozen, 2, POLICY, detached=False)
    assert shared == again
    # 未改写的视图就是Session里的Item；被引用策略改写的结果仍是新对象。
    assert [view is source for view, source in zip(shared.history, items, strict=True)] == [
        True,
        True,
        True,
        True,
        False,
    ]
