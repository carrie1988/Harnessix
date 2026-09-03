from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from harnessix.agent.approvals import request_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_approvals import REJECT, approval, reply


class ScopedTools(RecordingTools):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.scopes = []

    async def execute(self, call, cancel):
        raise AssertionError("显式 Scoped 入口不能回退到旧执行入口")

    async def execute_scoped(self, call, scope, cancel):
        scope.validate_call(call)
        self.scopes.append(scope)
        return await super().execute(call, cancel)


def test_ports_are_exclusive_before_reading_definitions(tmp_path):
    class NoDefinitions(ScopedTools):
        def definitions(self):
            pytest.fail("冲突配置不能选择或合并定义")

    with pytest.raises(KernelError) as error:
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            FakeProvider(),
            NoDefinitions(),
            scoped_tools=NoDefinitions(),
        )
    assert error.value.code == "tool_runtime_conflict"


async def test_legacy_selection_does_not_autodetect_scoped_method(tmp_path):
    class Legacy(RecordingTools):
        async def execute_scoped(self, *args):
            pytest.fail("旧入口不能自动升级")

    tools = Legacy()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        ScriptedProvider([tool_step("test.read"), answer()]),
        tools,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        assert (
            await runtime.run_turn(thread.thread_id, "旧接口", request_id="legacy")
        ).status == TurnStatus.COMPLETED
    assert len(tools.calls) == 1


async def test_scope_comes_from_durable_state_not_model_arguments(tmp_path):
    store = SQLiteSessionStore(tmp_path / "s.db")
    snapshots = []

    class Inspect(ScopedTools):
        async def execute_scoped(self, call, scope, cancel):
            snapshot = await store.get_thread(scope.thread_id)
            snapshots.append(snapshot)
            assert snapshot.active_turn_id == scope.turn_id
            assert snapshot.turns[-1].status == TurnStatus.EXECUTING_TOOLS
            assert scope.request_fingerprint == request_fingerprint(
                snapshot, snapshot.turns[-1], call
            )
            with pytest.raises(FrozenInstanceError):
                scope.thread_id = uuid4()
            return await super().execute_scoped(call, scope, cancel)

    forged = {
        "thread_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "call_id": str(uuid4()),
        "workspace": "/not-authorized",
    }
    steps = [
        [
            ResponseStarted(response_id="call"),
            ToolCallCompleted(call_id="wire-call", tool="test.read", arguments=forged),
            ResponseCompleted(finish_reason="tool_calls"),
        ],
        answer(),
    ]
    tools = Inspect()
    provider = ScriptedProvider(steps)
    async with AgentRuntime(store, provider, scoped_tools=tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "不要信任参数归属", request_id="scope")
    assert turn.status == TurnStatus.COMPLETED
    scope = tools.scopes[0]
    assert scope.thread_id == thread.thread_id and scope.turn_id == turn.turn_id
    assert str(scope.call_id) != forged["call_id"] and scope.workspace == str(tmp_path)
    assert tools.calls[0].arguments == forged
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)
    assert scope.request_fingerprint not in "".join(e.model_dump_json() for e in events)
    assert scope.request_fingerprint not in provider.requests[-1].model_dump_json()
    with pytest.raises(KernelError):
        ToolExecutionScope.for_pending_call(
            await store.get_thread(thread.thread_id), turn.turn_id, tools.calls[0]
        )
    # 在执行中取得的完整快照可以重建同一上下文，但它不是终态后的发布许可证。
    assert ToolExecutionScope.for_pending_call(snapshots[0], turn.turn_id, tools.calls[0]) == scope


@pytest.mark.parametrize(
    "field", ["thread_id", "turn_id", "call_id", "workspace", "request_fingerprint"]
)
async def test_scope_fingerprint_detects_accidental_cross_call_binding(tmp_path, field):
    tools = ScopedTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        ScriptedProvider([tool_step("test.read"), answer()]),
        scoped_tools=tools,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "作用域", request_id="scope")
    changed = (
        "/other"
        if field == "workspace"
        else "0" * 64
        if field == "request_fingerprint"
        else uuid4()
    )
    with pytest.raises(KernelError) as error:
        replace(tools.scopes[0], **{field: changed}).validate_call(tools.calls[0])
    assert error.value.code == "tool_scope_mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("arguments", {"path": "changed"}),
        ("tool_version", "other"),
        ("tool_fingerprint", "0" * 64),
        ("requires_approval", True),
    ],
)
async def test_scope_detects_call_changes(tmp_path, field, value):
    tools = ScopedTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        ScriptedProvider([tool_step("test.read"), answer()]),
        scoped_tools=tools,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "作用域", request_id="scope")
    with pytest.raises(KernelError) as error:
        tools.scopes[0].validate_call(tools.calls[0].model_copy(update={field: value}))
    assert error.value.code == "tool_scope_mismatch"


async def test_scoped_type_error_is_not_retried_or_downgraded(tmp_path):
    class Broken(ScopedTools):
        async def execute_scoped(self, call, scope, cancel):
            self.scopes.append(scope)
            raise TypeError("PRIVATE /host/secret")

    tools = Broken()
    store = SQLiteSessionStore(tmp_path / "s.db")
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, scoped_tools=tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "异常", request_id="failure")
    assert turn.status == TurnStatus.FAILED and turn.error.code == "runtime_error"
    assert len(tools.scopes) == 1 and not tools.calls and len(provider.requests) == 1
    assert "PRIVATE" not in turn.model_dump_json()
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("decision", [None, REJECT])
async def test_scoped_approval_reopen_and_reject(tmp_path, decision):
    store = SQLiteSessionStore(tmp_path / "s.db")
    initial = ScopedTools(approval=True)
    steps = [tool_step("test.read"), answer()]
    async with AgentRuntime(store, ScriptedProvider(steps), scoped_tools=initial) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "审批", request_id="approval")
        assert turn.status == TurnStatus.WAITING_APPROVAL and initial.scopes == []
    tools = ScopedTools(approval=True)
    async with AgentRuntime(
        SQLiteSessionStore(store.path), ScriptedProvider(steps), scoped_tools=tools
    ) as runtime:
        if decision is None:
            await reply(runtime, thread.thread_id, turn)
        else:
            await reply(runtime, thread.thread_id, turn, decision)
        completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
        assert completed.status == TurnStatus.COMPLETED
        assert len(tools.scopes) == (1 if decision is None else 0)
        if tools.scopes:
            assert tools.scopes[0].request_fingerprint == approval(turn).request_fingerprint
            assert tools.scopes[0].call_id == approval(turn).call_id
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )


@pytest.mark.parametrize("kind", ["unknown", "write", "mismatch"])
async def test_scoped_dispatch_does_not_bypass_kernel_guards(tmp_path, kind):
    tools = ScopedTools(
        effect=EffectClass.DESTRUCTIVE if kind == "write" else EffectClass.READ_ONLY
    )
    store = SQLiteSessionStore(tmp_path / "s.db")

    class DriftRuntime(AgentRuntime):
        async def _execute_tool(self, thread_id, turn_id, call, token):
            if kind == "mismatch":
                call = call.model_copy(update={"tool_version": "changed"})
            return await super()._execute_tool(thread_id, turn_id, call, token)

    provider = ScriptedProvider(
        [tool_step("unknown" if kind == "unknown" else "test.read"), answer()]
    )
    async with DriftRuntime(store, provider, scoped_tools=tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "门禁", request_id="guard")
    assert tools.scopes == []
    if kind == "mismatch":
        assert turn.status == TurnStatus.FAILED and turn.error.code == "tool_contract_changed"
    else:
        results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
        assert results[0].error.code == (
            "unknown_tool" if kind == "unknown" else "tool_not_enabled"
        )


async def test_parallel_threads_multiple_calls_and_later_turns_do_not_share_scope(tmp_path):
    both = asyncio.Event()

    class Rendezvous(ScopedTools):
        async def execute_scoped(self, call, scope, cancel):
            result = await super().execute_scoped(call, scope, cancel)
            if len(self.scopes) >= 2:
                both.set()
            await asyncio.wait_for(both.wait(), 10)
            scope.validate_call(call)
            return result

    store = SQLiteSessionStore(tmp_path / "s.db")
    tools = Rendezvous()
    provider = ScriptedProvider([tool_step("test.read", "test.read"), answer()])
    async with AgentRuntime(store, provider, scoped_tools=tools) as runtime:
        threads = [await runtime.create_thread(str(tmp_path / name)) for name in ("one", "two")]
        turns = await asyncio.gather(
            *(runtime.run_turn(t.thread_id, "并发", request_id="first") for t in threads)
        )
        turns.append(await runtime.run_turn(threads[0].thread_id, "下一轮", request_id="second"))
    assert all(t.status == TurnStatus.COMPLETED for t in turns)
    assert len(tools.scopes) == len({s.call_id for s in tools.scopes}) == 6
    for turn in turns:
        scopes = [s for s in tools.scopes if s.turn_id == turn.turn_id]
        assert len(scopes) == 2 and len({s.thread_id for s in scopes}) == 1
        snapshot = await store.get_thread(scopes[0].thread_id)
        assert all(s.workspace == snapshot.workspace for s in scopes)
        assert replay(await store.events(snapshot.thread_id)) == snapshot


@pytest.mark.parametrize("kind", ["inactive", "waiting", "other_turn", "altered_call", "settled"])
async def test_scope_factory_requires_live_unsettled_matching_call(tmp_path, kind):
    store = SQLiteSessionStore(tmp_path / "s.db")
    captured = []

    class Capture(ScopedTools):
        async def execute_scoped(self, call, scope, cancel):
            captured.append(await store.get_thread(scope.thread_id))
            return await super().execute_scoped(call, scope, cancel)

    tools = Capture()
    async with AgentRuntime(
        store, ScriptedProvider([tool_step("test.read"), answer()]), scoped_tools=tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "工厂校验", request_id="factory")
    snapshot, call = captured[0], tools.calls[0]
    turn, turn_id = snapshot.turns[-1], completed.turn_id
    if kind == "inactive":
        snapshot = snapshot.model_copy(update={"active_turn_id": None})
    elif kind == "waiting":
        snapshot = snapshot.model_copy(
            update={"turns": (turn.model_copy(update={"status": TurnStatus.WAITING_APPROVAL}),)}
        )
    elif kind == "other_turn":
        turn_id = uuid4()
    elif kind == "altered_call":
        call = call.model_copy(update={"arguments": {"modified": True}})
    else:
        snapshot = snapshot.model_copy(
            update={"turns": (turn.model_copy(update={"items": completed.items}),)}
        )
    with pytest.raises(KernelError) as error:
        ToolExecutionScope.for_pending_call(snapshot, turn_id, call)
    assert error.value.code == ("turn_not_found" if kind == "other_turn" else "tool_scope_mismatch")
