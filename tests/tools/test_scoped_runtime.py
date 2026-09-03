from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.agent.approvals import execution_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools import files, search
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.agent.test_approvals import reply
from tests.tools.test_files import call
from tests.tools.test_search_kernel import tool_step


@pytest.mark.parametrize("tool", ["list_files", "read_file", "glob", "grep"])
async def test_old_approval_can_resume_through_explicit_scoped_coding_runtime(tmp_path, tool):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("读取夹具")
    store = SQLiteSessionStore(tmp_path / "s.db")
    steps = [tool_step(tool), answer()]
    async with CodingToolRuntime(root, require_approval=True) as tools:
        definitions = tools.definitions()
        async with AgentRuntime(store, ScriptedProvider(steps), tools) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "旧接口审批", request_id="old")
            assert turn.status == TurnStatus.WAITING_APPROVAL
    async with CodingToolRuntime(root, require_approval=True) as tools:
        assert tools.definitions() == definitions
        async with AgentRuntime(
            SQLiteSessionStore(store.path), ScriptedProvider(steps), scoped_tools=tools
        ) as runtime:
            await reply(runtime, thread.thread_id, turn)
            completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert completed.status == TurnStatus.COMPLETED
            results = [
                i.content for i in completed.items if isinstance(i.content, ToolResultContent)
            ]
            assert len(results) == 1 and results[0].outcome == "succeeded"


@pytest.mark.parametrize("kind", ["other_root", "alias", "fingerprint", "arguments"])
async def test_coding_scope_mismatch_fails_before_target_io(tmp_path, monkeypatch, kind):
    def forbidden(*args):
        pytest.fail("作用域不匹配时不得触碰目标文件")

    monkeypatch.setattr(files, "read_file", forbidden)
    async with CodingToolRuntime(tmp_path) as tools:
        request = call(tools, path="main.py")
        thread_id, turn_id = uuid4(), uuid4()
        workspace = str(tools.workspace_root)
        if kind == "other_root":
            workspace += "/different"
        elif kind == "alias":
            workspace += "/."
        scope = ToolExecutionScope(
            thread_id,
            turn_id,
            request.call_id,
            workspace,
            execution_fingerprint(thread_id, turn_id, workspace, request),
        )
        if kind == "fingerprint":
            scope = ToolExecutionScope(thread_id, turn_id, request.call_id, workspace, "0" * 64)
        elif kind == "arguments":
            request = request.model_copy(update={"arguments": {"path": "changed"}})
        with pytest.raises(KernelError) as error:
            await tools.execute_scoped(request, scope, CancelToken())
        assert error.value.code == (
            "tool_workspace_mismatch" if kind in {"other_root", "alias"} else "tool_scope_mismatch"
        )


@pytest.mark.parametrize(
    "tool,args", [("read_file", {"path": "main.py"}), ("grep", {"query": "读取"})]
)
async def test_model_cannot_inject_scope_through_coding_arguments(
    tmp_path, monkeypatch, tool, args
):
    def forbidden(*args):
        pytest.fail("无效参数不得执行工具")

    monkeypatch.setattr(files, "read_file", forbidden)
    monkeypatch.setattr(search, "grep", forbidden)
    root = tmp_path / "repo"
    root.mkdir()
    args = {**args, "thread_id": str(uuid4())}
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="call"),
                ToolCallCompleted(call_id="call", tool=tool, arguments=args),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer(),
        ]
    )
    async with CodingToolRuntime(root) as tools:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, scoped_tools=tools
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "拒绝归属参数", request_id="forged")
    results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert len(results) == 1 and results[0].error.code == "tool_invalid_arguments"
