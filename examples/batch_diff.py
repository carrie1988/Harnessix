"""真实 Session 审批与写效果的只读差异报告；尚不发布 Artifact。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    PatchBatchApprovalRequestContent,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import records
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace


async def exercise(root):
    source_path = root / "source"
    source_path.mkdir()
    paths = ("one.py", "two.py")
    for path in paths:
        (source_path / path).write_text("before\n")
    factory = PatchWorkspaces(root / "private")
    store = SQLiteSessionStore(root / "session.db")
    with Workspace(source_path) as source, factory.create(source, paths, ReadOperation()) as copy:
        async with ManagedPatchBatchBridge(copy) as bridge:
            proposal = PatchBatchProposal(
                files=tuple(
                    PatchProposal(
                        path=path,
                        expected_revision=read_file(
                            copy.workspace, ReadFileInput(path=path), ReadOperation()
                        ).revision,
                        edits=(ExactEdit(old_text="before", new_text="after"),),
                    )
                    for path in paths
                )
            )
            provider = ScriptedProvider(
                [
                    [
                        ResponseStarted(response_id="edit"),
                        ToolCallCompleted(
                            call_id="group",
                            tool="apply_patch_batch",
                            arguments=proposal.model_dump(mode="json"),
                        ),
                        ResponseCompleted(finish_reason="tool_calls"),
                    ],
                    [
                        ResponseStarted(response_id="done"),
                        TextStarted(content_id="done"),
                        TextCompleted(content_id="done", text="副本修改完成"),
                        ResponseCompleted(),
                    ],
                ]
            )
            async with AgentRuntime(store, provider, patch_batches=bridge) as runtime:
                thread = await runtime.create_thread(str(copy.workspace.root))
                waiting = await runtime.run_turn(
                    thread.thread_id, "修改并展示两文件", request_id="diff"
                )
                request = next(
                    i.content
                    for i in waiting.items
                    if isinstance(i.content, PatchBatchApprovalRequestContent)
                )
                call = next(
                    i.content for i in waiting.items if isinstance(i.content, ToolCallContent)
                )
                plan = request.plan
                scope = ToolExecutionScope(
                    plan.thread_id,
                    plan.turn_id,
                    plan.call_id,
                    str(copy.workspace.root),
                    plan.call_fingerprint,
                )
                planned = await bridge.diff(call, scope, plan, CancelToken())
                assert planned.document.summary.view == "plan"
                assert all((copy.workspace.root / p).read_text() == "before\n" for p in paths)
                decided = await runtime.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="差异报告验收宿主"
                    ),
                )
                record = next(
                    i.content.decision
                    for i in decided.items
                    if isinstance(i.content, PatchBatchApprovalRequestContent)
                )
                completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                assert completed.status == "completed"
                effect = next(
                    i.content.patch_batch
                    for i in completed.items
                    if isinstance(i.content, ToolResultContent)
                )
                events = await store.events(thread.thread_id)
                history = await bridge.diff(
                    call,
                    scope,
                    plan,
                    CancelToken(),
                    view="effect",
                    approval=record,
                    execution=effect.execution,
                )
                assert history.document.summary.effect == "applied"
                assert planned.document.edits == history.document.edits
                assert await store.events(thread.thread_id) == events
                assert replay(events) == await store.get_thread(thread.thread_id)
                for report in (planned, history):
                    assert report.document.summary.complete
                    assert len(records(report.document.to_jsonl())) == 5
                assert all((copy.workspace.root / p).read_text() == "after\n" for p in paths)
    assert all((source_path / p).read_text() == "before\n" for p in paths)
    print("真实 Session 计划/审批/两文件写入与历史差异报告通过；JSONL 有界且不改变事件或源目录。")
    print("报告尚未发布为 Artifact；未调用真实模型 API。")


def main():
    with TemporaryDirectory(prefix="harnessix-batch-diff-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
