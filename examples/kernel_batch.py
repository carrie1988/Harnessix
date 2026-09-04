"""离线两文件读取→整组持久审批重开→顺序写入→读回；不调用模型 API。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.models import PatchBatchApprovalRequestContent, ToolResultContent
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
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.managed import PatchWorkspaces
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace

PATHS = ("one.py", "two.py")


class BatchFixtureProvider:
    async def stream(self, request, cancel):
        cancel.checkpoint()
        results = [i.content for i in request.history if isinstance(i.content, ToolResultContent)]
        assert all(result.outcome == "succeeded" for result in results)
        yield ResponseStarted(response_id=f"batch-{request.step}")
        if request.step in (1, 2, 4, 5):
            index = (request.step - 1) if request.step < 3 else (request.step - 4)
            tool, arguments = "read_file", {"path": PATHS[index]}
        elif request.step == 3:
            assert all(result.output["text"] == "before\n" for result in results)
            tool = "apply_patch_batch"
            arguments = {
                "files": [
                    {
                        "path": path,
                        "expected_revision": result.output["revision"],
                        "edits": [{"old_text": "before", "new_text": "after"}],
                    }
                    for path, result in zip(PATHS, results, strict=True)
                ]
            }
        else:
            assert request.step == 6 and results[2].output["effect"] == "applied"
            assert all(result.output["text"] == "after\n" for result in results[-2:])
            yield TextStarted(content_id="answer")
            yield TextCompleted(content_id="answer", text="两文件已在副本修改并读回。")
            yield ResponseCompleted()
            return
        yield ToolCallCompleted(call_id=f"batch-{request.step}", tool=tool, arguments=arguments)
        yield ResponseCompleted(finish_reason="tool_calls")


async def exercise(root: Path) -> None:
    source_path = root / "source"
    source_path.mkdir()
    for path in PATHS:
        (source_path / path).write_text("before\n")
    factory = PatchWorkspaces(root / "private")
    store = SQLiteSessionStore(root / "session.db")
    with Workspace(source_path) as source:
        with factory.create(source, PATHS, ReadOperation()) as copy:
            workspace_id = copy.workspace_id
            async with (
                ManagedPatchBatchBridge(copy) as bridge,
                CodingToolRuntime(copy.workspace.root) as reads,
                AgentRuntime(
                    store, BatchFixtureProvider(), scoped_tools=reads, patch_batches=bridge
                ) as runtime,
            ):
                thread = await runtime.create_thread(str(copy.workspace.root))
                waiting = await runtime.run_turn(
                    thread.thread_id, "同时修改两文件并读回", request_id="batch"
                )
                assert waiting.status == "waiting_approval"
                approval = next(
                    i.content
                    for i in waiting.items
                    if isinstance(i.content, PatchBatchApprovalRequestContent)
                )
                assert (
                    ManagedPatchBatches(copy)
                    .get(approval.plan.backend.batch_id, ReadOperation())
                    .decision
                    is None
                )
        with factory.open(workspace_id) as copy:
            async with (
                ManagedPatchBatchBridge(copy) as bridge,
                CodingToolRuntime(copy.workspace.root) as reads,
                AgentRuntime(
                    store, BatchFixtureProvider(), scoped_tools=reads, patch_batches=bridge
                ) as runtime,
            ):
                await runtime.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="离线整组验收宿主"
                    ),
                )
                assert (
                    ManagedPatchBatches(copy)
                    .get(approval.plan.backend.batch_id, ReadOperation())
                    .decision
                    is None
                )
                completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                assert completed.status == "completed"
                effect = next(
                    i.content.patch_batch
                    for i in completed.items
                    if isinstance(i.content, ToolResultContent) and i.content.patch_batch
                )
                assert effect.execution.effect == "applied"
            assert all((copy.workspace.root / path).read_text() == "after\n" for path in PATHS)
        assert all((source_path / path).read_text() == "before\n" for path in PATHS)
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
    print("两文件读取、整组审批重开、一次顺序写、读回及 Replay 通过；源目录未写，无模型 API。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-kernel-batch-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
