from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.models.scripted import ScriptedProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.output_artifact import parse_process_output_document
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer


@pytest.mark.parametrize(
    "point,published",
    [
        ("after_insert", False),
        ("before_commit", False),
        ("after_commit", True),
    ],
)
async def test_real_exit_recovers_process_output_without_action_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    published: bool,
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
            "tests.artifacts.process_output_crash_worker",
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
    assert child.returncode == 87, child.stderr
    assert marker.read_text() == "1"

    session = SQLiteSessionStore(session_path)
    await session.initialize()
    thread_id = (await session.thread_ids())[0]
    before = await session.get_thread(thread_id)
    assert before.turns[-1].status is (
        TurnStatus.EXECUTING_TOOLS if published else TurnStatus.WAITING_ACTION
    )
    with sqlite3.connect(session.path) as database:
        assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == int(
            published
        )

    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(effects_path),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await actions.initialize()
    bridge = ProcessAgentBridge(
        actions,
        Principal(
            tenant_id="tenant-a",
            subject_id="agent-a",
            framework="harnessix-agent",
        ),
    )
    artifacts = SQLiteArtifactStore(session)
    provider = ScriptedProvider([] if published else [[], answer("恢复后完成")])

    async def forbidden(*args, **kwargs):
        pytest.fail("恢复不得重新准备、批准或执行Process Action")

    monkeypatch.setattr(bridge, "prepare", forbidden)
    monkeypatch.setattr(bridge, "decide", forbidden)
    if published:
        monkeypatch.setattr(bridge, "observe", forbidden)
    try:
        async with CodingToolRuntime(root, artifacts=artifacts) as tools:
            publisher = SQLiteProcessArtifactPublisher(
                artifacts, bridge, workspace_scope=tools.workspace_scope
            )
            async with AgentRuntime(
                session,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                processes=bridge,
                process_artifacts=publisher,
            ) as runtime:
                current = (await session.get_thread(thread_id)).turns[-1]
                final = (
                    current if published else await runtime.resume_turn(thread_id, current.turn_id)
                )
                assert final.status is (
                    TurnStatus.INTERRUPTED if published else TurnStatus.COMPLETED
                ), final.error
            result = next(
                item.content
                for item in final.items
                if isinstance(item.content, ToolResultContent) and item.content.process is not None
            )
            assert isinstance(result.output, dict)
            ref = UUID(result.output["artifact"]["artifact_id"])
            page = await artifacts.read(thread_id, tools.workspace_scope, ref, limit=200)
            document = parse_process_output_document(page.text.encode())
            streams = {"stdout": bytearray(), "stderr": bytearray()}
            for chunk in document.chunks:
                streams[chunk.stream].extend(chunk.data())
            assert bytes(streams["stdout"]) == b"crash-out-\x00\xff"
            assert bytes(streams["stderr"]) == b"crash-err"
    finally:
        await actions.close()

    assert len(provider.requests) == int(not published)
    assert marker.read_text() == "1"
    with sqlite3.connect(effects_path) as database:
        assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
        assert database.execute("SELECT status FROM actions").fetchone()[0] == "succeeded"
    with sqlite3.connect(session.path) as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
    assert replay(await session.events(thread_id)) == await session.get_thread(thread_id)
