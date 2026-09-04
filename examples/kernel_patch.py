"""离线验证读→提案→持久审批重开→受管写→读回；不是自主编码 Eval。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.models import PatchApprovalRequestContent, ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.managed import PatchWorkspaces
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace


class PatchFixtureProvider:
    async def stream(self, request, cancel):
        cancel.checkpoint()
        outputs = [i.content for i in request.history if isinstance(i.content, ToolResultContent)]
        assert all(result.outcome == "succeeded" for result in outputs)
        yield ResponseStarted(response_id=f"patch-{request.step}")
        if request.step in (1, 3):
            if request.step == 3:
                assert outputs[-1].output["state"] == "applied"
            tool, args = "read_file", {"path": "main.py"}
        elif request.step == 2:
            assert outputs[-1].output["text"] == 'print("before")\n'
            tool, args = (
                "apply_patch",
                {
                    "path": "main.py",
                    "expected_revision": outputs[-1].output["revision"],
                    "edits": [{"old_text": "before", "new_text": "after"}],
                },
            )
        else:
            assert request.step == 4 and outputs[-1].output["text"] == 'print("after")\n'
            yield TextStarted(content_id="answer")
            yield TextCompleted(content_id="answer", text="副本修改完成并已读回；源目录未改。")
            yield ResponseCompleted()
            return
        yield ToolCallCompleted(call_id=f"patch-{request.step}", tool=tool, arguments=args)
        yield ResponseCompleted(finish_reason="tool_calls")


async def exercise(directory: Path) -> None:
    source = directory / "source"
    source.mkdir()
    (source / "main.py").write_text('print("before")\n', encoding="utf-8")
    factory = PatchWorkspaces(directory / "private")
    store = SQLiteSessionStore(directory / "session.db")
    with Workspace(source) as workspace:
        with factory.create(workspace, ["main.py"], ReadOperation()) as copy:
            workspace_id = copy.workspace_id
            async with (
                ManagedPatchBridge(copy) as bridge,
                CodingToolRuntime(copy.workspace.root) as reads,
                AgentRuntime(
                    store, PatchFixtureProvider(), scoped_tools=reads, patches=bridge
                ) as runtime,
            ):
                thread = await runtime.create_thread(str(copy.workspace.root))
                waiting = await runtime.run_turn(
                    thread.thread_id, "修改并读回 main.py", request_id="patch"
                )
                assert waiting.status == TurnStatus.WAITING_APPROVAL
                approval = next(
                    i.content
                    for i in waiting.items
                    if isinstance(i.content, PatchApprovalRequestContent)
                )
                assert copy.get(approval.plan.plan_id).state == "pending"
        # 所有权按 Kernel、桥接/只读工具、副本顺序释放，然后重开原副本/Session。
        with factory.open(workspace_id) as copy:
            async with (
                ManagedPatchBridge(copy) as bridge,
                CodingToolRuntime(copy.workspace.root) as reads,
                AgentRuntime(
                    store, PatchFixtureProvider(), scoped_tools=reads, patches=bridge
                ) as runtime,
            ):
                await runtime.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="离线验收宿主"
                    ),
                )
                assert copy.get(approval.plan.plan_id).state == "pending"
                completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                assert completed.status == TurnStatus.COMPLETED
                assert copy.get(approval.plan.plan_id).state == "applied"
            assert (copy.workspace.root / "main.py").read_text() == 'print("after")\n'
    assert (source / "main.py").read_text() == 'print("before")\n'
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
    print("读取、持久写审批重开、一次受管写入、读回及 Replay 通过；无模型 API、无源目录写入。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-kernel-patch-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
