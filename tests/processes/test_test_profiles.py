from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ProcessApprovalRequestContent, ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    Principal,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.processes.test_profiles import RunTestsAgentBridge
from harnessix.processes.test_profiles import TestProfile as Profile
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.worker import ActionWorker
from tests.agent.helpers import answer


def _step(profile: str, **extra):
    return [
        ResponseStarted(response_id="tests"),
        ToolCallCompleted(
            call_id="run-tests-call",
            tool="run_tests",
            arguments={"profile": profile, **extra},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def _fixture(root: Path, profile: Profile) -> tuple[ActionService, RunTestsAgentBridge]:
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(root.parent / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await service.initialize()
    bridge = RunTestsAgentBridge(
        service,
        Principal(
            tenant_id="tenant-a",
            subject_id="agent-a",
            framework="harnessix-agent",
        ),
        root,
        (profile,),
    )
    return service, bridge


def _approval(turn) -> ProcessApprovalRequestContent:
    return next(
        item.content
        for item in turn.items
        if isinstance(item.content, ProcessApprovalRequestContent)
    )


async def test_run_tests_only_exposes_profile_and_reports_test_failure(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    marker = tmp_path / "count"
    code = (
        "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        "print('FAILED fixed profile'); raise SystemExit(1)"
    )
    profile = Profile(
        name="unit",
        description="固定离线单元测试",
        program="python",
        arguments=("-I", "-c", code, str(marker)),
        timeout_seconds=5,
    )
    service, bridge = await _fixture(root, profile)
    provider = ScriptedProvider([_step("unit"), answer("测试失败已反馈")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    try:
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            thread = await runtime.create_thread(str(root))
            pending = await runtime.run_turn(thread.thread_id, "运行单元测试", request_id="tests")
            request = _approval(pending)
            assert pending.status is TurnStatus.WAITING_APPROVAL and not marker.exists()
            assert {tool.name for tool in provider.requests[0].tools} == {"run_tests"}
            public = provider.requests[0].tools[0].model_dump_json()
            assert str(marker) not in public and code not in public
            assert set(provider.requests[0].tools[0].input_schema["properties"]) == {"profile"}

            action = await service.get(request.plan.action_id)
            assert action.status is ActionStatus.PENDING_APPROVAL
            assert action.request.tool == "host.process"
            assert action.request.arguments["arguments"] == ["-I", "-c", code, str(marker)]

            waiting = await runtime.reply_approval(
                thread.thread_id,
                pending.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="test-reviewer"),
            )
            assert waiting.status is TurnStatus.WAITING_ACTION and not marker.exists()

        completed_action = await ActionWorker(service, poll_seconds=0.01).run_once()
        assert completed_action is not None
        assert completed_action.status is ActionStatus.SUCCEEDED

        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            completed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
        assert completed.status is TurnStatus.COMPLETED and marker.read_text() == "1"
        result = next(
            item.content
            for item in completed.items
            if isinstance(item.content, ToolResultContent) and item.content.process is not None
        )
        assert result.outcome == "succeeded"
        assert result.output["profile"] == "unit" and result.output["passed"] is False
        assert result.output["returncode"] == 1
    finally:
        await service.close()


@pytest.mark.parametrize(
    "profile,extra,code",
    [
        ("missing", {}, "test_profile_not_found"),
        ("unit", {"arguments": ["--injected"]}, "tool_invalid_arguments"),
    ],
)
async def test_run_tests_rejects_unknown_profile_and_model_arguments(
    tmp_path: Path, profile: str, extra: dict[str, object], code: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    service, bridge = await _fixture(
        root,
        Profile(
            name="unit",
            description="固定测试",
            program="python",
            arguments=("-I", "-c", "print('ok')"),
            timeout_seconds=5,
        ),
    )
    try:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"),
            ScriptedProvider([_step(profile, **extra), answer("已拒绝无效测试调用")]),
            processes=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            completed = await runtime.run_turn(thread.thread_id, "测试", request_id="invalid")
        assert completed.status is TurnStatus.COMPLETED
        result = next(
            item.content for item in completed.items if isinstance(item.content, ToolResultContent)
        )
        assert result.outcome == "failed" and result.error.code == code
        with sqlite3.connect(tmp_path / "effects.db") as database:
            assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    finally:
        await service.close()


def test_run_tests_rejects_workspace_and_profile_binding_drift(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    principal = Principal(tenant_id="t", subject_id="s", framework="f")
    profile = Profile(
        name="unit", description="测试", program="python", arguments=(), timeout_seconds=5
    )
    with pytest.raises(KernelError) as mismatch:
        RunTestsAgentBridge(service, principal, other, (profile,))
    assert mismatch.value.code == "test_workspace_mismatch"
    with pytest.raises(KernelError) as invalid:
        RunTestsAgentBridge(
            service,
            principal,
            root,
            (profile.model_copy(update={"program": "missing"}),),
        )
    assert invalid.value.code == "test_profiles_invalid"


async def test_run_tests_fails_closed_when_thread_workspace_does_not_match(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    service, bridge = await _fixture(
        root,
        Profile(
            name="unit",
            description="固定测试",
            program="python",
            arguments=("-I", "-c", "print('must not run')"),
            timeout_seconds=5,
        ),
    )
    try:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"),
            ScriptedProvider([_step("unit")]),
            processes=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(other))
            failed = await runtime.run_turn(thread.thread_id, "测试", request_id="wrong-root")
        assert failed.status is TurnStatus.INTERRUPTED
        assert failed.error is not None and failed.error.code == "uncertain_effect"
        with sqlite3.connect(tmp_path / "effects.db") as database:
            assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    finally:
        await service.close()
