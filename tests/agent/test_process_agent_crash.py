from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.models import ProcessApprovalRequestContent, ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ActionStatus, ApprovalDecision, ApprovalOutcome, Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.models.scripted import ScriptedProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.worker import ActionWorker
from tests.agent.helpers import answer
from tests.agent.test_process_agent_runtime import _process_step

_POINTS = (
    "runtime.before_process_action_prepare",
    "runtime.after_process_action_prepare",
    "runtime.after_approval_request",
    "runtime.before_approval_decision",
    "runtime.after_process_action_decision",
    "runtime.after_approval_decision",
    "runtime.after_process_action_observe",
    "runtime.after_process_action_result",
)


def _approval(turn) -> ProcessApprovalRequestContent:
    return next(
        item.content
        for item in turn.items
        if isinstance(item.content, ProcessApprovalRequestContent)
    )


async def _service(root: Path, effects_path: Path) -> tuple[ActionService, ProcessAgentBridge]:
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(effects_path),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await service.initialize()
    return service, ProcessAgentBridge(
        service,
        Principal(
            tenant_id="tenant-a",
            subject_id="agent-a",
            framework="harnessix-agent",
        ),
    )


@pytest.mark.parametrize("point", _POINTS)
async def test_real_exit_recovers_cross_store_process_saga_without_replay(
    tmp_path: Path,
    point: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    session_path = tmp_path / "session.db"
    effects_path = tmp_path / "effects.db"
    marker = tmp_path / "execution-count"
    child = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "tests.agent.process_agent_crash_worker",
            str(root),
            str(session_path),
            str(effects_path),
            str(marker),
            point,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=Path(__file__).parents[2],
    )
    assert child.returncode == 88, child.stderr

    store = SQLiteSessionStore(session_path)
    if point == "runtime.before_process_action_prepare":
        async with AgentRuntime(store, ScriptedProvider([])):
            thread_id = (await store.thread_ids())[0]
            preserved = (await store.get_thread(thread_id)).turns[-1]
            assert preserved.status is TurnStatus.EXECUTING_TOOLS
        with sqlite3.connect(effects_path) as database:
            assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    service, bridge = await _service(root, effects_path)
    artifacts = SQLiteArtifactStore(store)
    provider = ScriptedProvider([] if point.endswith("result") else [(), answer("恢复完成")])
    try:
        async with CodingToolRuntime(root, artifacts=artifacts) as tools:
            publisher = SQLiteProcessArtifactPublisher(
                artifacts, bridge, workspace_scope=tools.workspace_scope
            )
            async with AgentRuntime(
                store,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                processes=bridge,
                process_artifacts=publisher,
            ) as runtime:
                thread_id = (await store.thread_ids())[0]
                turn = (await store.get_thread(thread_id)).turns[-1]
                if turn.status is TurnStatus.WAITING_APPROVAL:
                    approval = _approval(turn)
                    if approval.decision is None:
                        action = await service.get(approval.plan.action_id)
                        if action.approval is not None:
                            turn = await runtime.resume_turn(thread_id, turn.turn_id)
                        else:
                            turn = await runtime.reply_approval(
                                thread_id,
                                turn.turn_id,
                                approval.approval_id,
                                fingerprint=approval.request_fingerprint,
                                decision=ApprovalDecision(
                                    outcome=ApprovalOutcome.APPROVED,
                                    actor="recovery-fixture",
                                ),
                            )
                    else:
                        turn = await runtime.resume_turn(thread_id, turn.turn_id)
                if turn.status is TurnStatus.WAITING_ACTION:
                    action = await service.get(_approval(turn).plan.action_id)
                    if action.status is ActionStatus.READY:
                        completed = await ActionWorker(
                            service,
                            poll_seconds=0.01,
                            heartbeat_seconds=1,
                            recovery_interval_seconds=1,
                        ).run_once()
                        assert completed is not None
                    turn = await runtime.resume_turn(thread_id, turn.turn_id)
                final = turn
                assert final.status is (
                    TurnStatus.INTERRUPTED
                    if point == "runtime.after_process_action_result"
                    else TurnStatus.COMPLETED
                )
                saved = await store.get_thread(thread_id)
                assert replay(await store.events(thread_id)) == saved
    finally:
        await service.close()

    assert marker.read_text() == "1"
    assert len(provider.requests) == int(point != "runtime.after_process_action_result")
    with sqlite3.connect(effects_path) as database:
        assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
        assert database.execute("SELECT status FROM actions").fetchone()[0] == "succeeded"
    with sqlite3.connect(session_path) as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
    results = [
        item.content
        for item in final.items
        if isinstance(item.content, ToolResultContent) and item.content.process is not None
    ]
    assert len(results) == 1 and results[0].outcome == "succeeded"


async def _race_decisions(
    root: Path,
    session_path: Path,
    effects_path: Path,
    barrier: Path,
    decisions: tuple[tuple[str, str], tuple[str, str]],
) -> list[dict[str, object]]:
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.agent.process_decision_worker",
            str(root),
            str(session_path),
            str(effects_path),
            str(barrier),
            outcome,
            actor,
            cwd=Path(__file__).parents[2],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for outcome, actor in decisions
    ]
    await asyncio.to_thread(barrier.write_text, "go")
    completed = await asyncio.gather(*(process.communicate() for process in processes))
    results = []
    for process, (stdout, stderr) in zip(processes, completed, strict=True):
        assert process.returncode == 0, stderr.decode()
        results.append(json.loads(stdout))
    return results


@pytest.mark.parametrize(
    ("decisions", "successes"),
    [
        ((("approved", "reviewer-a"), ("approved", "reviewer-a")), 2),
        ((("approved", "reviewer-a"), ("rejected", "reviewer-b")), 1),
    ],
)
async def test_cross_process_decision_race_has_one_authoritative_action_fact(
    tmp_path: Path,
    decisions: tuple[tuple[str, str], tuple[str, str]],
    successes: int,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    session_path = tmp_path / "session.db"
    effects_path = tmp_path / "effects.db"
    provider = ScriptedProvider([_process_step("print('not-run')"), answer()])
    store = SQLiteSessionStore(session_path)
    service, bridge = await _service(root, effects_path)
    try:
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            thread = await runtime.create_thread(str(root))
            pending = await runtime.run_turn(
                thread.thread_id,
                "竞争审批",
                request_id="cross-process-decision",
            )
            request = _approval(pending)

        results = await _race_decisions(
            root,
            session_path,
            effects_path,
            tmp_path / "decision-barrier",
            decisions,
        )
        assert sum(result["ok"] is True for result in results) == successes
        if successes == 1:
            loser = next(result for result in results if result["ok"] is False)
            assert loser["code"] == "approval_conflict"

        action = await service.get(request.plan.action_id)
        assert action.approval is not None
        events = await service.events(request.plan.action_id)
        decision_events = [
            event
            for event in events
            if event.event_type in {"approval_granted", "approval_rejected"}
        ]
        assert len(decision_events) == 1
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            synchronized = await runtime.resume_turn(thread.thread_id, pending.turn_id)
        assert synchronized.status is TurnStatus.WAITING_ACTION
        assert _approval(synchronized).decision == action.approval
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )
    finally:
        await service.close()
