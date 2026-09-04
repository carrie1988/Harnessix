"""宿主整组调用闭环；不是模型工具或 Session 审批示例。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from harnessix.agent.approvals import execution_fingerprint, tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace


async def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source_path = root / "source"
        source_path.mkdir()
        paths = ("one.py", "two.py")
        for path in paths:
            (source_path / path).write_text("before\n")
        factory = PatchWorkspaces(root / "private")
        with Workspace(source_path) as source:
            with factory.create(source, paths, ReadOperation()) as copy:
                workspace_id = copy.workspace_id
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
                    definition = bridge.definition()
                    call = ToolCallContent(
                        call_id=uuid4(),
                        provider_call_id="host-batch",
                        tool=definition.name,
                        tool_version=definition.version,
                        effect_class=definition.effect_class,
                        requires_approval=True,
                        tool_fingerprint=tool_fingerprint(definition),
                        arguments=proposal.model_dump(mode="json"),
                    )
                    thread_id, turn_id = uuid4(), uuid4()
                    workspace = str(copy.workspace.root)
                    scope = ToolExecutionScope(
                        thread_id,
                        turn_id,
                        call.call_id,
                        workspace,
                        execution_fingerprint(thread_id, turn_id, workspace, call),
                    )
                    plan = await bridge.prepare(call, scope, CancelToken())
                    await bridge.review(call, scope, plan, CancelToken())
                    decision = ApprovalRecord(
                        request_fingerprint=plan.approval_fingerprint,
                        outcome=ApprovalOutcome.APPROVED,
                        actor="示例宿主",
                    )
                    # 仅宿主夹具授权；真实 Session 持久消费示例见 kernel_batch.py。
                    result = await bridge.execute(call, scope, plan, decision, CancelToken())
                    assert result.execution is not None and result.execution.effect == "applied"
            with factory.open(workspace_id) as reopened:
                async with ManagedPatchBatchBridge(reopened) as bridge:
                    recovered = await bridge.recover(
                        call, scope, CancelToken(), plan=plan, approval=decision
                    )
                    assert recovered == result
                    assert all(
                        (reopened.workspace.root / p).read_text() == "after\n" for p in paths
                    )
            assert all((source_path / p).read_text() == "before\n" for p in paths)
    print("整组宿主桥接：完整调用批准、两文件顺序应用、重开只核对、源目录不变。")


if __name__ == "__main__":
    asyncio.run(main())
