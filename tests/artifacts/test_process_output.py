from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ProcessApprovalRequestContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.contracts import ArtifactPolicy
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    Principal,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.models._history import messages_for
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.contracts import ProcessResult, ProcessStream
from harnessix.processes.output_artifact import (
    ProcessOutputDocument,
    ProcessOutputRecord,
    parse_process_output_document,
    process_output_document,
)
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.worker import ActionWorker
from tests.agent.helpers import answer


def _stream(data: bytes, *, observed: bytes | None = None, eof: bool = True) -> ProcessStream:
    full = data if observed is None else observed
    return ProcessStream(
        data_base64=base64.b64encode(data).decode("ascii"),
        captured_bytes=len(data),
        observed_bytes=len(full),
        observed_sha256=hashlib.sha256(full).hexdigest(),
        truncated=len(full) > len(data),
        eof=eof,
    )


def _result(stdout: bytes, stderr: bytes = b"") -> ProcessResult:
    return ProcessResult(
        pid=123,
        returncode=0,
        stop_reason="exited",
        termination="none",
        stdout=_stream(stdout),
        stderr=_stream(stderr),
        elapsed_seconds=0.1,
    )


def _process_step(code: str) -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="process-output"),
        ToolCallCompleted(
            call_id="process-output",
            tool="host.process",
            arguments={
                "program": "python",
                "arguments": ["-I", "-c", code],
                "timeout_seconds": 5.0,
            },
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def _approval(turn: Turn) -> ProcessApprovalRequestContent:
    return next(
        item.content
        for item in turn.items
        if isinstance(item.content, ProcessApprovalRequestContent)
    )


async def _exercise(
    tmp_path: Path,
    *,
    fault=None,
    policy: ArtifactPolicy | None = None,
) -> tuple[
    SQLiteSessionStore,
    SQLiteArtifactStore,
    str,
    ActionService,
    ScriptedProvider,
    UUID,
    Turn,
]:
    root = tmp_path / "repo"
    root.mkdir()
    session = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(session, policy=policy, fault=fault)
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "effects.db"),
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
    provider = ScriptedProvider(
        [
            _process_step(
                "import os; os.write(1, b'out-' + bytes([0, 255])); os.write(2, '错误\\n'.encode())"
            ),
            answer("正文已归档"),
        ]
    )
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
            thread = await runtime.create_thread(str(tools.workspace_root))
            pending = await runtime.run_turn(
                thread.thread_id, "执行并归档", request_id="process-output"
            )
            request = _approval(pending)
            waiting = await runtime.reply_approval(
                thread.thread_id,
                pending.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="reviewer-a",
                ),
            )
            assert waiting.status is TurnStatus.WAITING_ACTION
            completed_action = await ActionWorker(
                actions,
                poll_seconds=0.01,
                heartbeat_seconds=1,
                recovery_interval_seconds=1,
            ).run_once()
            assert completed_action is not None
            completed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
        scope = tools.workspace_scope
    return session, artifacts, scope, actions, provider, thread.thread_id, completed


def test_process_output_document_is_binary_safe_and_canonical() -> None:
    process = _result(b"a\x00\xff" * 5000, "中文\n".encode())
    document = process_output_document(process)
    assert document is not None and document.summary.complete
    body = document.to_jsonl()
    restored = parse_process_output_document(body)
    assert restored == document
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    for chunk in restored.chunks:
        streams[chunk.stream].extend(chunk.data())
    assert bytes(streams["stdout"]) == process.stdout.data()
    assert bytes(streams["stderr"]) == process.stderr.data()
    assert max(map(len, body.splitlines(keepends=True))) <= 24 * 1024
    assert b'"pid"' not in body and b"action_id" not in body and b"arguments" not in body
    assert TypeAdapter(ProcessOutputRecord).validate_json(body.splitlines()[0])


def test_process_output_document_does_not_hide_second_truncation() -> None:
    too_large = _result(b"x" * 400_000, b"y" * 400_000)
    assert process_output_document(too_large) is None
    incomplete = ProcessResult(
        **_result(b"abc").model_dump(exclude={"stdout"}),
        stdout=_stream(b"abc", observed=b"abcdef", eof=False),
    )
    document = process_output_document(incomplete)
    assert document is not None and not document.summary.complete


def test_process_output_document_rejects_tampered_chunks() -> None:
    document = process_output_document(_result(b"abcdef"))
    assert document is not None
    data = document.model_dump(mode="json")
    data["chunks"][0]["offset"] = 1
    with pytest.raises(ValidationError):
        ProcessOutputDocument.model_validate(data)
    with pytest.raises(ValueError, match="损坏"):
        parse_process_output_document(document.to_jsonl().replace(b'"offset":0', b'"offset":1'))


@pytest.mark.parametrize(
    "name,schema",
    [
        ("process-output-record", TypeAdapter(ProcessOutputRecord).json_schema()),
        ("process-output-document", ProcessOutputDocument.model_json_schema()),
    ],
)
def test_process_output_schema_is_checked_in(name: str, schema: dict[str, object]) -> None:
    path = Path(__file__).parents[2] / "spec" / f"{name}-v1.schema.json"
    assert json.loads(path.read_text()) == schema


async def test_process_output_is_published_with_terminal_session_facts(tmp_path: Path) -> None:
    session, artifacts, scope, actions, provider, thread_id, completed = await _exercise(tmp_path)
    try:
        assert completed.status is TurnStatus.COMPLETED
        result = next(
            item.content
            for item in completed.items
            if isinstance(item.content, ToolResultContent) and item.content.process is not None
        )
        assert isinstance(result.output, dict)
        ref = result.output["artifact"]
        offset, pages = 0, []
        while True:
            page = await artifacts.read(
                thread_id,
                scope,
                UUID(ref["artifact_id"]),
                offset=offset,
                limit=1,
            )
            pages.append(page.text)
            if page.next_offset is None:
                break
            offset = page.next_offset
        assert len(pages) == 3
        document = parse_process_output_document("".join(pages).encode())
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        for chunk in document.chunks:
            streams[chunk.stream].extend(chunk.data())
        assert bytes(streams["stdout"]) == b"out-\x00\xff"
        assert bytes(streams["stderr"]) == "错误\n".encode()
        assert page.artifact.complete and page.next_offset is None
        with sqlite3.connect(session.path) as database:
            assert database.execute("SELECT purpose FROM agent_artifacts").fetchone() == (
                "process_output",
            )
        history = json.dumps(messages_for(provider.requests[1]), ensure_ascii=False)
        assert "process_output/v1" not in history
        assert "data_base64" not in history
        assert str(result.action_id) not in history
    finally:
        await actions.close()


@pytest.mark.parametrize("point", ["process_output.after_insert", "process_output.before_commit"])
async def test_publish_failure_preserves_process_result_without_reference(
    tmp_path: Path, point: str
) -> None:
    def fault(name: str) -> None:
        if name == point:
            raise RuntimeError("PRIVATE artifact failure")

    session, _, _, actions, _, _, completed = await _exercise(tmp_path, fault=fault)
    try:
        assert completed.status is TurnStatus.COMPLETED
        result = next(
            item.content
            for item in completed.items
            if isinstance(item.content, ToolResultContent) and item.content.process is not None
        )
        assert isinstance(result.output, dict) and "artifact" not in result.output
        assert "PRIVATE" not in completed.model_dump_json()
        with sqlite3.connect(session.path) as database:
            assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0
    finally:
        await actions.close()


async def test_after_commit_loss_keeps_single_reference(tmp_path: Path) -> None:
    def fault(name: str) -> None:
        if name == "process_output.after_commit":
            raise RuntimeError("PRIVATE lost acknowledgement")

    session, artifacts, scope, actions, _, thread_id, completed = await _exercise(
        tmp_path, fault=fault
    )
    try:
        result = next(
            item.content
            for item in completed.items
            if isinstance(item.content, ToolResultContent) and item.content.process is not None
        )
        assert isinstance(result.output, dict) and "artifact" in result.output
        ref = UUID(result.output["artifact"]["artifact_id"])
        assert (await artifacts.read(thread_id, scope, ref)).artifact.artifact_id == ref
        with sqlite3.connect(session.path) as database:
            assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
    finally:
        await actions.close()


async def test_quota_omits_archive_without_erasing_effect(tmp_path: Path) -> None:
    session, _, _, actions, _, _, completed = await _exercise(
        tmp_path, policy=ArtifactPolicy(max_turn_bytes=1)
    )
    try:
        result = next(
            item.content
            for item in completed.items
            if isinstance(item.content, ToolResultContent) and item.content.process is not None
        )
        assert completed.status is TurnStatus.COMPLETED
        assert isinstance(result.output, dict) and "artifact" not in result.output
        with sqlite3.connect(session.path) as database:
            assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0
    finally:
        await actions.close()


@pytest.mark.parametrize("kind", ["body", "manifest", "call", "reference", "purpose"])
async def test_process_output_corruption_is_not_empty_success(tmp_path: Path, kind: str) -> None:
    session, artifacts, scope, actions, _, thread_id, completed = await _exercise(tmp_path)
    try:
        result = next(
            item.content
            for item in completed.items
            if isinstance(item.content, ToolResultContent) and item.content.process is not None
        )
        assert isinstance(result.output, dict)
        ref = UUID(result.output["artifact"]["artifact_id"])
        with sqlite3.connect(session.path) as database:
            if kind == "body":
                body = database.execute("SELECT body FROM agent_artifacts").fetchone()[0]
                database.execute(
                    "UPDATE agent_artifacts SET body = ?, size_bytes = ?",
                    (body.replace(b'"offset":0', b'"offset":1', 1), len(body)),
                )
            elif kind == "manifest":
                manifest = json.loads(
                    database.execute("SELECT manifest_json FROM agent_artifacts").fetchone()[0]
                )
                manifest["records"] += 1
                database.execute(
                    "UPDATE agent_artifacts SET manifest_json = ?",
                    (json.dumps(manifest),),
                )
            elif kind == "call":
                database.execute("UPDATE agent_artifacts SET call_id = ?", (str(uuid4()),))
            elif kind == "purpose":
                database.execute("UPDATE agent_artifacts SET purpose = 'tool_result'")
            else:
                snapshot = json.loads(
                    database.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
                )
                for turn in snapshot["turns"]:
                    for item in turn["items"]:
                        content = item["content"]
                        if (
                            content.get("kind") == "tool_result"
                            and content.get("process") is not None
                        ):
                            content["output"]["artifact"]["artifact_id"] = str(uuid4())
                encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                database.execute(
                    "UPDATE agent_threads SET snapshot_json = ?, snapshot_sha256 = ?",
                    (encoded, hashlib.sha256(encoded.encode()).hexdigest()),
                )
        with pytest.raises(KernelError) as error:
            await artifacts.read(thread_id, scope, ref)
        assert error.value.code == "artifact_corrupt"
    finally:
        await actions.close()


async def test_process_output_expiry_and_owner_boundary(tmp_path: Path, monkeypatch) -> None:
    _, artifacts, scope, actions, _, thread_id, completed = await _exercise(tmp_path)
    try:
        result = next(
            item.content
            for item in completed.items
            if isinstance(item.content, ToolResultContent) and item.content.process is not None
        )
        assert isinstance(result.output, dict)
        ref = UUID(result.output["artifact"]["artifact_id"])
        for denied_thread_id, workspace_scope in (
            (uuid4(), scope),
            (thread_id, "0" * 64),
        ):
            with pytest.raises(KernelError) as error:
                await artifacts.read(denied_thread_id, workspace_scope, ref)
            assert error.value.code == "artifact_not_found"
        expires_at = result.output["artifact"]["expires_at"]
        future = artifact_sqlite.utc_now() + timedelta(days=8)
        assert future.isoformat() > expires_at
        monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)
        with pytest.raises(KernelError) as error:
            await artifacts.read(thread_id, scope, ref)
        assert error.value.code == "artifact_expired"
        assert (await artifacts.collect()).expired == 1
    finally:
        await actions.close()


async def test_process_publisher_must_match_runtime_ports(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    session = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(session)
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await actions.initialize()
    bridge = ProcessAgentBridge(
        actions,
        Principal(tenant_id="t", subject_id="s", framework="test"),
    )
    other = ProcessAgentBridge(
        actions,
        Principal(tenant_id="t", subject_id="s", framework="test"),
    )
    publisher = SQLiteProcessArtifactPublisher(artifacts, other, workspace_scope="0" * 64)
    try:
        with pytest.raises(KernelError) as error:
            AgentRuntime(
                session,
                ScriptedProvider([]),
                processes=bridge,
                process_artifacts=publisher,
            )
        assert error.value.code == "artifact_store_mismatch"
    finally:
        await actions.close()
