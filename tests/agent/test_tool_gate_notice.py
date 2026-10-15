"""工具门禁说明：视图给出的受限原因只进入本次目录说明，不改变可执行集合。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.agent.models import (
    Item,
    ItemStatus,
    ToolCallContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.runtime import AgentRuntime
from harnessix.models._chat_mapping import build_request
from harnessix.models.config import OpenAIChatConfig
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer
from tests.agent.test_stable_tool_catalog import RecordingProvider, call
from tests.contracts.provider import model_request

NOTICE = "patch reopens after read_file of pkg/core.py"


class NoticeView:
    def __init__(self, notice):
        self.notice = notice

    def select(self, turn, definitions):
        return ModelToolSelection(definitions, notice=self.notice)


async def test_runtime_passes_the_view_notice_to_the_request(tmp_path):
    provider = RecordingProvider([call("test.read"), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        provider,
        RecordingTools(),
        model_tool_view=NoticeView(NOTICE),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="n")
    assert turn.status is TurnStatus.COMPLETED
    assert [request.tool_notice for request in provider.seen] == [NOTICE, NOTICE]


@pytest.mark.parametrize("notice", ["", "x" * 1025, 7])
async def test_invalid_view_notice_is_rejected(tmp_path, notice):
    provider = RecordingProvider([answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        provider,
        RecordingTools(),
        model_tool_view=NoticeView(notice),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="n")
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "model_tool_view_invalid"
    assert provider.seen == []


def _catalog_note(request) -> str:
    body, _ = build_request(request, OpenAIChatConfig(model="m"))
    return next(
        (
            str(message["content"])
            for message in body["messages"]
            if "Current request tool catalog" in str(message["content"])
        ),
        "",
    )


def _request_with_tool_history():
    base = model_request(with_tools=True)
    (tool,) = base.tools
    call_id = uuid4()
    items = (
        Item(
            item_id=uuid4(),
            status=ItemStatus.COMPLETED,
            content=ToolCallContent(
                call_id=call_id,
                provider_call_id="p1",
                tool=tool.name,
                tool_version=tool.version,
                effect_class=tool.effect_class,
                requires_approval=False,
                arguments={},
            ),
        ),
        Item(
            item_id=uuid4(),
            status=ItemStatus.COMPLETED,
            content=ToolResultContent(call_id=call_id, outcome="succeeded", output={}),
        ),
    )
    return base.model_copy(update={"history": (*base.history, *items)})


def test_catalog_note_carries_the_notice_only_when_set():
    request = _request_with_tool_history()
    assert "Why the catalog is restricted" not in _catalog_note(request)
    noted = request.model_copy(update={"tool_notice": NOTICE})
    assert f"Why the catalog is restricted now: {NOTICE}" in _catalog_note(noted)
