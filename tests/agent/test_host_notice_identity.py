"""公开user_message的来源：Harnessix自动说明与用户输入可按结构区分（ADR 0173，Beta Issue #46）。

0.10.0b53：重复命令提示与操作者的原始任务导出后都是只有kind与text的user_message，按结构统计人工
介入次数得到2，实际只有1。判定按派生身份，不看正文前缀。
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from harnessix.agent.completion_guard import host_notice_events
from harnessix.agent.host_notice_identity import is_host_notice
from harnessix.agent.models import AgentEvent, ItemFinished, ItemStatus, TextContent
from harnessix.agent.step_notices import step_notice_item_id
from harnessix.agent.truncation_recovery import recovery_notice_item_id
from harnessix.protocol.projection import project_event

SOURCE = Path(__file__).resolve().parents[2] / "src" / "harnessix"
REGISTRY = (SOURCE / "agent" / "host_notice_identity.py").read_text(encoding="utf-8")


def test_every_module_that_emits_host_notices_is_registered() -> None:
    """新增一种宿主说明时须登记身份规则：调用host_notice_events的模块都要出现在登记表里。"""
    emitters = {
        path.stem
        for path in SOURCE.rglob("*.py")
        if "host_notice_events(" in path.read_text(encoding="utf-8")
        and path.name not in {"completion_guard.py", "host_notice_identity.py"}
    }
    assert emitters, "应能找到发出宿主说明的模块"
    for module in emitters:
        assert re.search(rf"from harnessix\.agent\.{module} import", REGISTRY), module


def test_host_notices_are_recognised_by_identity_not_by_text() -> None:
    turn_id = uuid4()
    assert is_host_notice(turn_id, step_notice_item_id(turn_id, 7))
    assert is_host_notice(turn_id, recovery_notice_item_id(turn_id, 1))
    assert not is_host_notice(turn_id, uuid4())
    assert not is_host_notice(None, step_notice_item_id(turn_id, 7))
    other_turn = uuid4()
    assert not is_host_notice(other_turn, step_notice_item_id(turn_id, 7))


def _public(turn_id, payload):
    thread_id = uuid4()
    event = AgentEvent(thread_id=thread_id, sequence=5, turn_id=turn_id, payload=payload)
    public = project_event(thread_id, event)
    assert public is not None
    return public.data.item.content


def test_public_user_messages_say_where_they_came_from() -> None:
    """修改前两条都只有kind与text。用户输入同样的提示文字仍是user。"""
    turn_id = uuid4()
    text = "[Harnessix host notice] `node check.js` has now run 3 times with exit code 0"
    started, finished = host_notice_events(turn_id, step_notice_item_id(turn_id, 3), text)
    for draft in (started, finished):
        notice = _public(turn_id, draft.payload)
        assert notice.origin == "harnessix" and notice.text == text
    typed = ItemFinished(
        item_id=uuid4(),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="user_message", text=text),
    )
    assert _public(turn_id, typed).origin == "user"
    reply = ItemFinished(
        item_id=uuid4(),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="assistant_message", text="好"),
    )
    assert _public(turn_id, reply).origin is None
