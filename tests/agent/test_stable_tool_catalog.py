"""稳定工具目录：线上目录整轮不变，本步可执行集仍由视图决定；越权调用被拒绝且不执行。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent, ToolCallRejectionContent, TurnStatus
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ToolDescriptor
from harnessix.models._chat_mapping import build_request
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer
from tests.contracts.provider import model_request


class TwoTools(RecordingTools):
    def definitions(self):
        (base,) = super().definitions()
        return (
            base,
            ToolDescriptor(**{**base.model_dump(), "name": "test.other"}),
        )


class OnlyFirst:
    """模拟阶段收窄：本步只允许test.read。"""

    def select(self, turn, definitions):
        return ModelToolSelection(tuple(d for d in definitions if d.name == "test.read"))


class RecordingProvider(ScriptedProvider):
    def __init__(self, steps):
        super().__init__(steps)
        self.seen = []

    def stream(self, request, cancel):
        self.seen.append(request)
        return super().stream(request, cancel)


def call(tool):
    return [
        ResponseStarted(response_id="r"),
        ToolCallCompleted(call_id="c", tool=tool, arguments={}),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


@pytest.mark.parametrize("enabled", [True, False])
async def test_catalog_is_stable_and_out_of_phase_calls_are_rejected(tmp_path, enabled):
    tools = TwoTools()
    provider = RecordingProvider([call("test.other"), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        provider,
        tools,
        model_tool_view=OnlyFirst(),
        stable_tool_catalog=enabled,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="s")
    assert turn.status is TurnStatus.COMPLETED
    first = provider.seen[0]
    assert [t.name for t in first.tools] == ["test.read"]
    expected_catalog = ["test.read", "test.other"] if enabled else ["test.read"]
    assert [t.name for t in first.wire_tools] == expected_catalog
    # 不论目录是否稳定，越权调用都不执行。
    assert tools.calls == []
    assert any(isinstance(i.content, ToolCallRejectionContent) for i in turn.items)
    assert not any(isinstance(i.content, ToolCallContent) for i in turn.items)


def test_mapping_sends_the_catalog_but_lists_only_callable_tools():
    base = model_request(with_tools=True)
    (tool,) = base.tools
    other = ToolDescriptor(**{**tool.model_dump(), "name": "test.other"})
    request = base.model_copy(update={"catalog_tools": (tool, other)})
    request = ModelRequest.model_validate(request.model_dump())
    body, names = build_request(request, OpenAIChatConfig(model="m"))
    assert sorted(names.values()) == ["test.other", tool.name]
    assert len(body["tools"]) == 2
    note = next(
        (
            m["content"]
            for m in body["messages"]
            if "Current request tool catalog" in str(m["content"])
        ),
        "",
    )
    if note:
        other_alias = next(a for a, n in names.items() if n == "test.other")
        assert other_alias not in note


def test_executable_tools_must_belong_to_the_catalog():
    base = model_request(with_tools=True)
    with pytest.raises(ValueError):
        ModelRequest.model_validate({**base.model_dump(), "catalog_tools": ()})


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_switch_requires_literal_boolean(tmp_path, value):
    with pytest.raises(KernelError):
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            RecordingTools(),
            stable_tool_catalog=value,
        )


def test_catalog_note_lists_only_the_callable_aliases():
    from harnessix.models._chat_mapping import _callable_aliases

    base = model_request(with_tools=True)
    (tool,) = base.tools
    other = ToolDescriptor(**{**tool.model_dump(), "name": "test.other"})
    request = base.model_copy(update={"catalog_tools": (tool, other)})
    _, names = build_request(request, OpenAIChatConfig(model="m"))
    callable_aliases = _callable_aliases(request, names)
    assert [names[a] for a in callable_aliases] == [tool.name]
