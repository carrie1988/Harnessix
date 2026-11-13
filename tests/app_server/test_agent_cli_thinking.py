"""无头过程输出里的思考预览：每一行以“思考：”开头，其后的输出另起一行。

0.10.0b24上的实测：思考内容与正文一样直接写出，没有标注也不换行，
`harnessix exec`的日志里出现“I'm going to read the README.md file.工具开始：read_file”。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from harnessix.agent.reasoning_preview import reasoning_item_id
from harnessix.agent_cli import ThinAgentCLI
from harnessix.protocol.contracts import (
    ItemPublicEvent,
    PublicEvent,
    PublicItem,
    PublicItemDelta,
    PublicTextContent,
    PublicToolCallContent,
    TurnStatePublicEvent,
)
from tests.app_server.test_agent_cli import ScriptedConsole

THREAD, TURN, REPLY = uuid4(), uuid4(), uuid4()


def _delta(item_id: UUID, text: str) -> PublicItemDelta:
    return PublicItemDelta(
        thread_id=THREAD, turn_id=TURN, item_id=item_id, model_step=1, stream_sequence=1, delta=text
    )


def _thinking(text: str) -> PublicItemDelta:
    return _delta(reasoning_item_id(TURN, 1), text)


def _event(data: ItemPublicEvent | TurnStatePublicEvent) -> PublicEvent:
    return PublicEvent(
        event_id=uuid4(),
        thread_id=THREAD,
        turn_id=TURN,
        cursor=1,
        occurred_at=datetime.now(UTC),
        data=data,
    )


def _tool_started(tool: str) -> PublicEvent:
    content = PublicToolCallContent(
        call_id=uuid4(),
        tool=tool,
        tool_version="1",
        effect_class="read_only",
        requires_approval=False,
    )
    item = PublicItem(item_id=uuid4(), status="started", content=content)
    return _event(ItemPublicEvent(type="item_started", item=item))


def _reply_finished(text: str) -> PublicEvent:
    content = PublicTextContent(kind="assistant_message", text=text)
    item = PublicItem(item_id=REPLY, status="completed", content=content)
    return _event(ItemPublicEvent(type="item_finished", item=item))


def _cli() -> tuple[ThinAgentCLI, ScriptedConsole]:
    console = ScriptedConsole(["2"])
    return ThinAgentCLI(SimpleNamespace(), console=console), console  # type: ignore[arg-type]


def _printed(console: ScriptedConsole) -> str:
    return "".join(text + end for text, end in console.output)


def test_thinking_is_labelled_and_the_tool_line_starts_on_its_own_line() -> None:
    cli, console = _cli()

    cli.render_delta(_thinking("I'm going to read "))
    cli.render_delta(_thinking("the README.md file."))
    cli._record_event(_tool_started("read_file"), render=True)

    assert _printed(console) == "思考：I'm going to read the README.md file.\n工具开始：read_file\n"


def test_the_reply_after_thinking_starts_on_its_own_line() -> None:
    cli, console = _cli()

    cli.render_delta(_thinking("只需回答一句话。"))
    cli.render_delta(_delta(REPLY, "第一行是"))
    cli.render_delta(_delta(REPLY, "标题。"))
    cli._record_event(_reply_finished("第一行是标题。"), render=True)

    assert _printed(console) == "思考：只需回答一句话。\n第一行是标题。\n"


def test_every_thinking_line_carries_the_label_and_blank_lines_are_dropped() -> None:
    cli, console = _cli()

    for fragment in ("\n先读文件。\n", "\n再回", "答。\n"):
        cli.render_delta(_thinking(fragment))
    cli._record_event(_event(TurnStatePublicEvent(status="executing_tools")), render=True)

    assert _printed(console) == "思考：先读文件。\n思考：再回答。\nTurn状态：executing_tools\n"


async def test_a_prompt_to_the_person_ends_the_thinking_line() -> None:
    cli, console = _cli()

    cli.render_delta(_thinking("需要用户选择环境。"))
    assert await cli.console.prompt("请选择：") == "2"

    assert _printed(console) == "思考：需要用户选择环境。\n请选择："


def test_a_reply_without_thinking_is_written_as_before() -> None:
    cli, console = _cli()

    cli.render_delta(_delta(REPLY, "完成"))
    cli._record_event(_reply_finished("完成"), render=True)

    assert _printed(console) == "完成\n"
