"""实现驱动公共Agent Protocol的薄命令行客户端。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Literal, Protocol
from uuid import UUID

from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    ItemPublicEvent,
    PublicApprovalDecision,
    PublicApprovalRequestContent,
    PublicEvent,
    PublicItem,
    PublicItemDelta,
    PublicPlanContent,
    PublicQuestionAnswerContent,
    PublicQuestionRequestContent,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolResultContent,
    QuestionRespondParams,
    ThreadView,
    TurnStatePublicEvent,
)
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, SubprocessAgentTransport

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def _event_page_limit(client: AgentClient) -> int:
    initialized = client.initialized
    return 256 if initialized is None else min(256, initialized.limits.max_replay_events)


class AgentConsole(Protocol):
    def write(self, text: str, *, end: str = "\n") -> None: ...

    async def prompt(self, text: str) -> str: ...


class StandardAgentConsole:
    def write(self, text: str, *, end: str = "\n") -> None:
        print(text, end=end, flush=True)

    async def prompt(self, text: str) -> str:
        return await asyncio.to_thread(input, text)


class ThinAgentCLI:
    """只消费Agent SDK的薄交互层；Session和Agent状态机仍归App Server。"""

    def __init__(
        self,
        client: AgentClient,
        *,
        console: AgentConsole | None = None,
        actor: str = "local-user",
    ) -> None:
        if not actor or len(actor) > 256:
            raise ValueError("CLI actor长度必须为1到256")
        self.client = client
        self.console = console or StandardAgentConsole()
        self.actor = actor
        self._items: dict[UUID, PublicItem] = {}
        self._rendered: set[UUID] = set()
        self._streamed_text: set[UUID] = set()
        self._gapped_text: set[UUID] = set()
        self._completed_text: set[UUID] = set()
        self._submitted_approvals: set[UUID] = set()
        self._submitted_questions: set[UUID] = set()

    async def _render_artifact(self, thread_id: UUID, item: PublicItem) -> None:
        content = item.content
        if not isinstance(content, PublicApprovalRequestContent):
            return
        reference = content.diff_artifact
        if reference is None:
            return
        self.console.write(f"差异 Artifact {reference.artifact_id}，{reference.records} 条记录：")
        offset = 0
        while True:
            page = await self.client.read_artifact(
                thread_id,
                reference.artifact_id,
                offset=offset,
                limit=100,
            )
            for line in page.text.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    value = line
                self.console.write(json.dumps(value, ensure_ascii=False, indent=2))
            if page.next_offset is None:
                break
            offset = page.next_offset

    def _record_event(self, event: PublicEvent, *, render: bool) -> None:
        data = event.data
        if isinstance(data, ItemPublicEvent):
            self._items[data.item.item_id] = data.item
            if not render:
                return
            content = data.item.content
            if isinstance(content, PublicTextContent):
                if (
                    content.kind == "assistant_message"
                    and data.type == "item_finished"
                    and data.item.item_id not in self._completed_text
                ):
                    if (
                        data.item.item_id in self._streamed_text
                        and data.item.item_id not in self._gapped_text
                    ):
                        self.console.write("")
                    else:
                        self.console.write(content.text)
                    self._completed_text.add(data.item.item_id)
                return
            if data.item.item_id in self._rendered:
                return
            if isinstance(content, PublicPlanContent):
                self.console.write("计划：")
                for step in content.steps:
                    self.console.write(f"- [{step.status}] {step.description}")
            elif isinstance(content, PublicToolCallContent):
                self.console.write(f"工具开始：{content.tool}")
            elif isinstance(content, PublicToolResultContent) and data.type == "item_finished":
                self.console.write(f"工具结果：{content.outcome}")
            elif isinstance(content, PublicApprovalRequestContent):
                self.console.write("需要审批。")
            elif isinstance(content, PublicQuestionRequestContent):
                self.console.write(f"需要输入：{content.question}")
            self._rendered.add(data.item.item_id)
        elif render and isinstance(data, TurnStatePublicEvent):
            self.console.write(f"Turn状态：{data.status}")

    async def _load_replay(self, thread_id: UUID, *, display_after: int) -> int:
        cursor, limit = 0, _event_page_limit(self.client)
        while True:
            page = await self.client.replay_events(thread_id, after_cursor=cursor, limit=limit)
            for event in page.events:
                self._record_event(event, render=event.cursor > display_after)
            cursor = page.scanned_through
            if not page.has_more:
                return cursor

    async def _answer_pending(self, thread: ThreadView) -> bool:
        turn = thread.latest_turn
        if turn is None:
            return False
        if turn.status == "waiting_approval":
            approval_item = next(
                (
                    item
                    for item in reversed(tuple(self._items.values()))
                    if isinstance(item.content, PublicApprovalRequestContent)
                    and item.content.decision is None
                    and item.content.approval_id not in self._submitted_approvals
                ),
                None,
            )
            if approval_item is None:
                return False
            await self._render_artifact(thread.thread_id, approval_item)
            content = approval_item.content
            assert isinstance(content, PublicApprovalRequestContent)
            reply = (await self.console.prompt("批准该操作？[y/N] ")).strip().lower()
            outcome: Literal["approved", "rejected"] = (
                "approved" if reply in {"y", "yes"} else "rejected"
            )
            await self.client.respond_approval(
                ApprovalRespondParams(
                    request_id=f"cli-approval-{content.approval_id}",
                    thread_id=thread.thread_id,
                    turn_id=turn.turn_id,
                    approval_id=content.approval_id,
                    fingerprint=content.request_fingerprint,
                    decision=PublicApprovalDecision(outcome=outcome, actor=self.actor),
                )
            )
            self._submitted_approvals.add(content.approval_id)
            return True
        if turn.status == "waiting_input":
            answered = {
                item.content.question_id
                for item in self._items.values()
                if isinstance(item.content, PublicQuestionAnswerContent)
            }
            question_request = next(
                (
                    item.content
                    for item in reversed(tuple(self._items.values()))
                    if isinstance(item.content, PublicQuestionRequestContent)
                    and item.content.question_id not in answered
                    and item.content.question_id not in self._submitted_questions
                ),
                None,
            )
            if question_request is None:
                return False
            if question_request.options:
                for index, option in enumerate(question_request.options, 1):
                    self.console.write(f"{index}. {option}")
            answer = (await self.console.prompt(f"{question_request.question} ")).strip()
            if not answer:
                self.console.write("回答不能为空。")
                return False
            if question_request.options and answer.isdecimal():
                index = int(answer)
                if 1 <= index <= len(question_request.options):
                    answer = question_request.options[index - 1]
            await self.client.respond_question(
                QuestionRespondParams(
                    request_id=f"cli-question-{question_request.question_id}",
                    thread_id=thread.thread_id,
                    turn_id=turn.turn_id,
                    question_id=question_request.question_id,
                    answer=answer,
                )
            )
            self._submitted_questions.add(question_request.question_id)
            return True
        return False

    async def follow(self, thread_id: UUID, *, display_after: int = 0) -> ThreadView:
        await self.client.resume_thread(thread_id)
        cursor = await self._load_replay(thread_id, display_after=display_after)
        while True:
            thread = await self.client.get_thread(thread_id)
            latest = thread.latest_turn
            if latest is None:
                return thread
            if latest.status in _TERMINAL:
                await self._load_replay(thread_id, display_after=cursor)
                return await self.client.get_thread(thread_id)
            if await self._answer_pending(thread):
                continue
            page = await self.client.next_events(
                thread_id,
                after_cursor=cursor,
                wait_ms=30_000,
                limit=_event_page_limit(self.client),
            )
            for event in page.replay.events:
                self._record_event(event, render=True)
            cursor = max(cursor, page.replay.scanned_through)
            if page.live_gap:
                self._gapped_text.update(delta.item_id for delta in page.deltas)
                self.console.write("实时文本存在缺口，正在以持久事件恢复。")
            for delta in page.deltas:
                self.render_delta(delta)

    def render_delta(self, delta: PublicItemDelta) -> None:
        if delta.item_id in self._gapped_text or delta.item_id in self._completed_text:
            return
        self._streamed_text.add(delta.item_id)
        self.console.write(delta.delta, end="")

    async def run_turn(
        self,
        thread_id: UUID,
        prompt: str,
        *,
        request_id: str,
    ) -> ThreadView:
        before = await self.client.get_thread(thread_id)
        await self.client.start_turn(thread_id, prompt, request_id=request_id)
        return await self.follow(thread_id, display_after=before.cursor)

    async def retry_turn(
        self,
        thread_id: UUID,
        source_turn_id: UUID,
        *,
        request_id: str,
    ) -> ThreadView:
        before = await self.client.get_thread(thread_id)
        await self.client.retry_turn(
            thread_id,
            source_turn_id,
            request_id=request_id,
        )
        return await self.follow(thread_id, display_after=before.cursor)

    async def list_threads(self) -> None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = await self.client.list_threads(cursor=cursor, limit=200)
            for thread in page.threads:
                self.console.write(f"{thread.thread_id}\t{thread.workspace}")
            cursor = page.next_cursor
            if cursor is None:
                return
            if cursor in seen_cursors:
                raise AgentSDKError("pagination_stalled", "Thread列表游标没有前进")
            seen_cursors.add(cursor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix agent", description="Harnessix Code薄Agent CLI"
    )
    parser.add_argument("--server-program", required=True, help="Agent Protocol stdio服务程序")
    parser.add_argument(
        "--server-arg",
        action="append",
        default=[],
        help="传给服务程序的单个argv；可重复，短横线参数使用--server-arg=VALUE",
    )
    parser.add_argument(
        "--client-instance-id",
        type=UUID,
        required=True,
        help="跨重连保持不变的客户端实例UUID",
    )
    parser.add_argument("--actor", default="local-user", help="审批审计主体")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="创建Thread")
    create.add_argument("workspace")
    create.add_argument("--request-id", required=True)
    commands.add_parser("list", help="分页列出全部未归档Thread")
    run = commands.add_parser("run", help="开始Turn并持续跟随")
    run.add_argument("thread_id", type=UUID)
    run.add_argument("prompt")
    run.add_argument("--request-id", required=True)
    follow = commands.add_parser("follow", help="恢复并跟随Thread")
    follow.add_argument("thread_id", type=UUID)
    resume = commands.add_parser("resume", help="恢复并跟随Thread")
    resume.add_argument("thread_id", type=UUID)
    fork = commands.add_parser("fork", help="分叉Thread")
    fork.add_argument("source_thread_id", type=UUID)
    fork.add_argument("--through-turn-id", type=UUID)
    fork.add_argument("--request-id", required=True)
    archive = commands.add_parser("archive", help="归档Thread")
    archive.add_argument("thread_id", type=UUID)
    archive.add_argument("--reason")
    archive.add_argument("--request-id", required=True)
    retry = commands.add_parser("retry", help="重试终态Turn并跟随")
    retry.add_argument("thread_id", type=UUID)
    retry.add_argument("source_turn_id", type=UUID)
    retry.add_argument("--request-id", required=True)
    steer = commands.add_parser("steer", help="向活动Turn追加输入")
    steer.add_argument("thread_id", type=UUID)
    steer.add_argument("turn_id", type=UUID)
    steer.add_argument("text")
    steer.add_argument("--request-id", required=True)
    cancel = commands.add_parser("cancel", help="取消活动Turn")
    cancel.add_argument("thread_id", type=UUID)
    cancel.add_argument("turn_id", type=UUID)
    cancel.add_argument("--request-id", required=True)
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    command = (arguments.server_program, *arguments.server_arg)
    client = AgentClient(
        SubprocessAgentTransport(command),
        client_instance_id=arguments.client_instance_id,
        client_name="harnessix-thin-cli",
    )
    console = StandardAgentConsole()
    cli = ThinAgentCLI(client, console=console, actor=arguments.actor)
    try:
        await client.initialize()
        if arguments.command == "create":
            value = await client.create_thread(arguments.workspace, request_id=arguments.request_id)
            console.write(str(value.thread_id))
        elif arguments.command == "list":
            await cli.list_threads()
        elif arguments.command == "run":
            await cli.run_turn(
                arguments.thread_id,
                arguments.prompt,
                request_id=arguments.request_id,
            )
        elif arguments.command in {"follow", "resume"}:
            await cli.follow(arguments.thread_id)
        elif arguments.command == "fork":
            value = await client.fork_thread(
                arguments.source_thread_id,
                request_id=arguments.request_id,
                through_turn_id=arguments.through_turn_id,
            )
            console.write(str(value.thread_id))
        elif arguments.command == "archive":
            value = await client.archive_thread(
                arguments.thread_id,
                request_id=arguments.request_id,
                reason=arguments.reason,
            )
            console.write(str(value.thread_id))
        elif arguments.command == "retry":
            await cli.retry_turn(
                arguments.thread_id,
                arguments.source_turn_id,
                request_id=arguments.request_id,
            )
        elif arguments.command == "steer":
            await client.steer_turn(
                arguments.thread_id,
                arguments.turn_id,
                arguments.text,
                request_id=arguments.request_id,
            )
        elif arguments.command == "cancel":
            await client.cancel_turn(
                arguments.thread_id,
                arguments.turn_id,
                request_id=arguments.request_id,
            )
        return 0
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    raise SystemExit(asyncio.run(_run(arguments)))
