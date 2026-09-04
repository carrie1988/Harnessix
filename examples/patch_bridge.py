"""宿主绑定→真实读取→计划→批准→写入→读回→重开核对；不发模型请求。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from harnessix.agent.approvals import execution_fingerprint, tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace


def bound_call(definition, root, arguments, thread_id, turn_id):
    # 宿主夹具显式构造调用；生产接入须由 Kernel 从持久活跃状态产生作用域。
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="offline-fixture",
        tool=definition.name,
        tool_version=definition.version,
        effect_class=definition.effect_class,
        arguments=arguments,
        requires_approval=definition.requires_approval,
        tool_fingerprint=tool_fingerprint(definition),
    )
    return call, ToolExecutionScope(
        thread_id,
        turn_id,
        call.call_id,
        str(root),
        execution_fingerprint(thread_id, turn_id, str(root), call),
    )


async def exercise(copy):
    thread_id, turn_id = uuid4(), uuid4()
    root = copy.workspace.root
    async with ManagedPatchBridge(copy) as bridge, CodingToolRuntime(root) as reads:
        definition = next(d for d in reads.definitions() if d.name == "read_file")
        read_call, read_scope = bound_call(
            definition, root, {"path": "main.py"}, thread_id, turn_id
        )
        before = await reads.execute_scoped(read_call, read_scope, CancelToken())
        assert before.outcome == "succeeded"
        call, scope = bound_call(
            bridge.definition(),
            root,
            {
                "path": "main.py",
                "expected_revision": before.output["revision"],
                "edits": [{"old_text": "return a - b", "new_text": "return a + b"}],
            },
            thread_id,
            turn_id,
        )
        plan = await bridge.prepare(call, scope, CancelToken())
        assert await bridge.prepare(call, scope, CancelToken()) == plan
        assert (await bridge.review(call, scope, plan, CancelToken())).state == "pending"
        # 本示例由受信宿主批准；尚未写入 Agent v5 的只读审批事件。
        decision = ApprovalRecord(
            outcome=ApprovalOutcome.APPROVED,
            actor="离线验收宿主",
            request_fingerprint=plan.approval_fingerprint,
        )
        result = await bridge.execute(call, scope, plan, decision, CancelToken())
        assert result.result.outcome == "succeeded" and result.record.state == "applied"
        read_call, read_scope = bound_call(
            definition, root, {"path": "main.py"}, thread_id, turn_id
        )
        after = await reads.execute_scoped(read_call, read_scope, CancelToken())
        assert after.outcome == "succeeded" and "return a + b" in after.output["text"]
        return call, scope, plan, decision


async def observe(copy, call, scope, plan, decision):
    async with ManagedPatchBridge(copy) as bridge:
        recovered = await bridge.recover(call, scope, CancelToken(), plan=plan, approval=decision)
        assert recovered.plan == plan and recovered.result.outcome == "succeeded"


def main():
    from harnessix.patches.managed import PatchWorkspaces

    with TemporaryDirectory(prefix="harnessix-bridge-") as directory:
        base = Path(directory)
        source_path = base / "source"
        source_path.mkdir()
        source_file = source_path / "main.py"
        source_file.write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
        original = source_file.read_bytes()
        factory = PatchWorkspaces(base / "private")
        with Workspace(source_path) as source:
            with factory.create(source, ["main.py"], ReadOperation()) as copy:
                call, scope, plan, decision = asyncio.run(exercise(copy))
                target = copy.workspace.root / "main.py"
        before_recovery = target.stat()
        with factory.open(plan.workspace_id) as reopened:
            asyncio.run(observe(reopened, call, scope, plan, decision))
        after_recovery = target.stat()
        assert (before_recovery.st_ino, before_recovery.st_mtime_ns) == (
            after_recovery.st_ino,
            after_recovery.st_mtime_ns,
        )
        assert source_file.read_bytes() == original
        print("宿主 Patch 桥接验收通过：读取、计划绑定、批准、写入、读回、重开不重写；源文件不变。")
        print("本示例不是模型写工具或自主编码 Eval，Kernel 默认仍只读。")


if __name__ == "__main__":
    main()
