from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.agent.reducer import get_turn
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.public_output import (
    MAX_PROCESS_PREVIEW_STREAM_BYTES,
    PublicProcessOutputSummaryV2,
)
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.trusted_output import (
    TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES,
    build_trusted_process_output,
    parse_trusted_process_output,
    trusted_process_public_output,
)
from harnessix.product_config import process_action
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.product_config.process_action import ProductProcessActionExecutor
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions import agent_gateway_output
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.product_config.test_process_action import (
    CANARY,
    _CancellableRuntime,
    _fake_engine,
    _NoSecrets,
    _observation,
    _probe_runner,
    _profile,
    _TerminalRuntime,
    protected,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="复用固定Process Profile的POSIX Owner夹具"
)


class _TwoStreamTerminalRuntime(_TerminalRuntime):
    def __init__(self, capability: Any, *, stderr: bytes = b"", **kwargs: Any) -> None:
        super().__init__(capability, **kwargs)
        self.stderr = stderr

    async def run(self, *args: Any, **kwargs: Any) -> ProcessLease:
        lease = await super().run(*args, **kwargs)
        self.lease = ProcessLease.model_validate_json(
            lease.model_copy(update={"stderr": _observation(self.stderr)}).model_dump_json()
        )
        return self.lease

    async def output(self, _: object, stream: str) -> bytes:
        assert stream in {"stdout", "stderr"}
        return self.stdout if stream == "stdout" else self.stderr


@dataclass
class _ApprovedProduct:
    agent: AgentRuntime
    router: TrustedActionRouter
    artifacts: SQLiteArtifactStore
    sessions: SQLiteSessionStore
    provider: ScriptedProvider
    runtime: _TerminalRuntime
    gateway: RouterBackedAgentActionGateway
    workspace_scope: str
    thread_id: UUID
    turn_id: UUID
    plan_id: UUID

    async def resume(self) -> Any:
        return await self.agent.resume_turn(self.thread_id, self.turn_id)

    async def project_terminal(self) -> ToolResultContent:
        thread = await self.sessions.get_thread(self.thread_id)
        turn = get_turn(thread, self.turn_id)
        call = next(
            item.content for item in turn.items if isinstance(item.content, ToolCallContent)
        )
        approval = next(
            item.content
            for item in turn.items
            if isinstance(item.content, TrustedActionApprovalRequestContent)
        )
        return await self.gateway.execute(thread, turn, call, approval, CancelToken())

    def artifact_count(self) -> int:
        with sqlite3.connect(self.sessions.path) as database:
            return database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0]


@asynccontextmanager
async def _approved_product(
    root: Path,
    *,
    stdout: bytes = b"safe output\n",
    stderr: bytes = b"",
    returncode: int = 0,
    stop_reason: str = "exited",
    cancellable: bool = False,
) -> AsyncIterator[_ApprovedProduct]:
    workspace = root / "workspace"
    workspace.mkdir()
    profile = _profile(_fake_engine(root))
    secrets = _NoSecrets()
    # 探测与终态Owner复用原夹具；不启动容器，不替换批准或公开保护判定。
    async with PosixProcessSupervisor(root / "probe-owner") as supervisor:
        probe = probe_product_process_profile(
            profile, supervisor, secrets, probe_runner=_probe_runner
        )
        assert probe.reason_code == "verified" and probe.verified is not None
        capability = probe.verified.runtime.capability
        runtime = (
            _CancellableRuntime(capability)
            if cancellable
            else _TwoStreamTerminalRuntime(
                capability,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                stop_reason=stop_reason,
            )
        )
        verified = replace(probe.verified, runtime=cast(Any, runtime))

    with ExitStack() as cleanup, protected() as scope:
        sessions = SQLiteSessionStore(root / "sessions.db")
        artifacts = SQLiteArtifactStore(sessions, public_output_protection=scope)
        environment = build_fixed_product_action_environment(workspace)
        plans = SQLiteExecutionPlanStore(root / "plans.db")
        audit = SQLiteActionAuditStore(root / "audit.db")
        transactions = SQLiteWorkspaceTransactionStore(root / "delivery")
        leases = WorkspaceLeaseStore(root / "leases.db")
        for store in (plans, audit, transactions, leases):
            cleanup.callback(store.close)
        router = TrustedActionRouter(
            plans=plans, audit=audit, workspace_root=environment.workspace_root
        )
        provider = ScriptedProvider(
            [
                [
                    ResponseStarted(response_id="preview-response"),
                    ToolCallCompleted(
                        call_id="preview-call",
                        tool="run_profile.unit-tests",
                        arguments={"profile": "unit-tests", "selectors": ["tests/unit"]},
                    ),
                    ResponseCompleted(finish_reason="tool_calls"),
                ],
                answer("执行完成"),
            ]
        )
        async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
            composition = build_product_action_composition(
                build_product_action_config(
                    workspace_patch_enabled=False, process_profiles=(profile,)
                ),
                environment,
                router,
                transactions,
                leases,
                artifacts,
                artifact_workspace_scope=tools.workspace_scope,
                process_probes=(
                    ProductProcessProfileProbeResult(
                        profile=profile, reason_code="verified", verified=verified
                    ),
                ),
                secrets=secrets,
            )
            assert composition.gateway is not None
            assert [item.name for item in composition.catalog.definitions()] == [
                "run_profile.unit-tests"
            ]
            async with AgentRuntime(
                sessions,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                trusted_actions=composition.gateway,
                public_output_protection=scope,
            ) as agent:
                thread = await agent.create_thread(str(workspace))
                waiting = await agent.run_turn(
                    thread.thread_id, "运行固定测试Profile", request_id="diagnostic-preview"
                )
                approval = next(
                    item.content
                    for item in waiting.items
                    if isinstance(item.content, TrustedActionApprovalRequestContent)
                )
                assert waiting.status is TurnStatus.WAITING_APPROVAL
                assert approval.presentation == "process" and approval.diff_artifact is None
                assert runtime.run_calls == 0
                await agent.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="preview-reviewer"
                    ),
                )
                assert router.status(approval.plan_id).state == "ready"
                assert router.approval(approval.plan_id) is not None
                assert runtime.run_calls == 0
                yield _ApprovedProduct(
                    agent=agent,
                    router=router,
                    artifacts=artifacts,
                    sessions=sessions,
                    provider=provider,
                    runtime=runtime,
                    gateway=composition.gateway,
                    workspace_scope=tools.workspace_scope,
                    thread_id=thread.thread_id,
                    turn_id=waiting.turn_id,
                    plan_id=approval.plan_id,
                )


def _result(turn: Any) -> ToolResultContent:
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(results) == 1
    return results[0]


async def _read_archive(case: _ApprovedProduct, result: ToolResultContent) -> Any:
    assert isinstance(result.output, dict)
    public = {key: value for key, value in result.output.items() if key != "artifact"}
    reference = cast(dict[str, Any], result.output["artifact"])
    artifact_id = UUID(reference["artifact_id"])
    offset, pages = 0, []
    while True:
        page = await case.artifacts.read(
            case.thread_id, case.workspace_scope, artifact_id, offset=offset, limit=1
        )
        assert page.offset == offset and page.text.count("\n") == 1
        assert page.artifact.model_dump(mode="json") == reference
        pages.append(page.text.encode())
        if page.next_offset is None:
            break
        assert page.next_offset == offset + 1
        offset = page.next_offset
        assert len(pages) < reference["records"]
    body = b"".join(pages)
    assert len(pages) == reference["records"]
    assert len(pages) >= 2
    assert len(body) == reference["size_bytes"]
    assert hashlib.sha256(body).hexdigest() == reference["sha256"]
    document = parse_trusted_process_output(body)
    assert document.summary.version == "trusted-process-output/v1"
    assert b'"diagnostic_preview"' not in body
    assert case.runtime.lease is not None
    original = build_trusted_process_output(
        "unit-tests",
        case.runtime.lease,
        case.runtime.stdout,
        getattr(case.runtime, "stderr", b""),
    )
    assert body == original.to_jsonl()
    for stream in ("stdout", "stderr"):
        expected = (
            case.runtime.stdout if stream == "stdout" else getattr(case.runtime, "stderr", b"")
        )
        assert (
            b"".join(chunk.data() for chunk in document.chunks if chunk.stream == stream)
            == expected
        )
    assert public == trusted_process_public_output(
        document, include_preview=public["version"] == "trusted-process-output/v2"
    )
    event = case.router.events(case.plan_id)[-1]
    assert event.output_sha256 == canonical_digest(public)
    assert event.artifact_sha256 == reference["sha256"]
    assert result.trusted_action is not None
    assert result.trusted_action.artifact_sha256 == reference["sha256"]
    return document


async def _assert_no_replay(case: _ApprovedProduct, completed: Any) -> None:
    events = case.router.events(case.plan_id)
    assert await case.resume() == completed
    assert case.router.events(case.plan_id) == events
    assert case.runtime.run_calls == 1


@pytest.mark.parametrize(
    ("returncode", "stop_reason", "error_code"),
    [
        (0, "exited", None),
        (2, "exited", "process_nonzero_exit"),
        (-15, "timeout", "process_timeout"),
    ],
    ids=["success", "nonzero", "timeout"],
)
async def test_approved_product_publishes_v2_and_pages_original_v1_bytes(
    tmp_path: Path, returncode: int, stop_reason: str, error_code: str | None
) -> None:
    stdout = b"stdout diagnostic\n" + b"x" * (TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES + 17)
    stderr = b"stderr diagnostic\n" + b"y" * 1100
    async with _approved_product(
        tmp_path, stdout=stdout, stderr=stderr, returncode=returncode, stop_reason=stop_reason
    ) as case:
        completed = await case.resume()
        assert completed.status is TurnStatus.COMPLETED
        result = _result(completed)
        assert result.outcome == ("succeeded" if error_code is None else "failed")
        assert (result.error.code if result.error is not None else None) == error_code
        assert isinstance(result.output, dict)
        public = {key: value for key, value in result.output.items() if key != "artifact"}
        checked = PublicProcessOutputSummaryV2.model_validate(public)
        assert checked.returncode == returncode and checked.stop_reason == stop_reason
        for name, body in (("stdout", stdout), ("stderr", stderr)):
            preview = getattr(checked.diagnostic_preview, name)
            assert preview.text == body[:MAX_PROCESS_PREVIEW_STREAM_BYTES].decode()
            assert preview.size_bytes == MAX_PROCESS_PREVIEW_STREAM_BYTES
            assert preview.truncated is True
        await _read_archive(case, result)
        assert case.router.status(case.plan_id).state == result.outcome
        assert case.artifact_count() == 1 and len(case.provider.requests) == 2
        assert case.runtime.reconcile_calls == 0
        await _assert_no_replay(case, completed)


@pytest.mark.parametrize(
    ("stdout", "expected_text"),
    [
        (b"x" * 1023 + "中文\n".encode(), "x" * 1023),
        (b"x" * 1100 + b"\xff", None),
        (b"x" * 1100 + b"\x00", None),
        (b"", ""),
    ],
    ids=["utf8-boundary", "binary-after-preview", "control-after-preview", "empty-stdout"],
)
async def test_v2_preview_is_byte_bounded_without_rewriting_archived_streams(
    tmp_path: Path, stdout: bytes, expected_text: str | None
) -> None:
    async with _approved_product(tmp_path, stdout=stdout, stderr=b"diagnostic\n") as case:
        completed = await case.resume()
        assert completed.status is TurnStatus.COMPLETED
        result = _result(completed)
        assert isinstance(result.output, dict)
        public = {key: value for key, value in result.output.items() if key != "artifact"}
        preview = PublicProcessOutputSummaryV2.model_validate(public).diagnostic_preview.stdout
        size = len(expected_text.encode()) if expected_text is not None else 0
        assert preview.text == expected_text and preview.size_bytes == size
        assert preview.truncated == (size < len(stdout))
        await _read_archive(case, result)
        await _assert_no_replay(case, completed)


@pytest.mark.parametrize(
    ("returncode", "stop_reason", "error_code"),
    [
        (0, "exited", None),
        (2, "exited", "process_nonzero_exit"),
        (-15, "timeout", "process_timeout"),
    ],
    ids=["success-v1", "nonzero-v1", "timeout-v1"],
)
async def test_historical_v1_terminal_hash_recovers_without_upgrade_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stop_reason: str,
    error_code: str | None,
) -> None:
    async with _approved_product(tmp_path, returncode=returncode, stop_reason=stop_reason) as case:
        execute = ProductProcessActionExecutor.execute

        async def historical_execute(self: Any, route: Any, arguments: Any) -> Any:
            outcome = await execute(self, route, arguments)
            document = await self.output_document(route.execution.plan_id)
            return outcome.model_copy(update={"output": trusted_process_public_output(document)})

        # 只在真实批准执行的结果生成阶段模拟旧版本；不改审计链或认证。
        with monkeypatch.context() as historical:
            historical.setattr(ProductProcessActionExecutor, "execute", historical_execute)
            original = await case.router.execute(case.plan_id)
        assert isinstance(original.output, dict)
        assert original.output["version"] == "trusted-process-output/v1"
        events = case.router.events(case.plan_id)
        original_hash = canonical_digest(original.output)
        assert events[-1].output_sha256 == original_hash

        completed = await case.resume()
        assert completed.status is TurnStatus.COMPLETED
        result = _result(completed)
        assert isinstance(result.output, dict)
        assert result.output["version"] == "trusted-process-output/v1"
        assert "diagnostic_preview" not in result.output
        assert (result.error.code if result.error is not None else None) == error_code
        assert result.trusted_action is not None and result.trusted_action.origin == "execution"
        document = await _read_archive(case, result)
        assert (
            canonical_digest(trusted_process_public_output(document, include_preview=True))
            != original_hash
        )
        assert case.router.events(case.plan_id) == events
        assert case.runtime.reconcile_calls == 0 and case.artifact_count() == 1
        await _assert_no_replay(case, completed)


@pytest.mark.parametrize("tamper", ["text", "size", "truncated", "extra"])
async def test_changed_public_preview_cannot_match_v2_audit_or_fall_back_to_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    async with _approved_product(tmp_path) as case:
        outcome = await case.router.execute(case.plan_id)
        assert outcome.kind == "succeeded"
        events = case.router.events(case.plan_id)
        original = process_action.trusted_process_public_output

        def changed_preview(document: Any, **kwargs: Any) -> Any:
            public = original(document, **kwargs)
            if kwargs.get("include_preview"):
                preview = public["diagnostic_preview"]["stdout"]
                if tamper == "text":
                    preview["text"] = "X" + preview["text"][1:]
                elif tamper == "size":
                    preview["size_bytes"] += 1
                elif tamper == "truncated":
                    preview["truncated"] = not preview["truncated"]
                else:
                    preview["untrusted"] = "extra"
            return public

        monkeypatch.setattr(process_action, "trusted_process_public_output", changed_preview)
        with pytest.raises(KernelError) as caught:
            await case.project_terminal()
        assert caught.value.code == "trusted_action_output_mismatch"
        completed = await case.resume()
        assert completed.status is TurnStatus.INTERRUPTED
        assert completed.error.code == "uncertain_effect"
        assert _result(completed).outcome == "unknown" and _result(completed).output is None
        assert case.router.status(case.plan_id).state == "succeeded"
        assert case.router.events(case.plan_id) == events
        assert case.artifact_count() == 0 and len(case.provider.requests) == 1
        assert case.runtime.run_calls == 1 and case.runtime.reconcile_calls == 0
        await _assert_no_replay(case, completed)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("location", ["outside-preview", "across-chunks"])
async def test_full_body_secret_protection_rejects_canary_beyond_preview(
    tmp_path: Path, stream: str, location: str
) -> None:
    prefix = 1100 if location == "outside-preview" else TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES - 8
    payload = b"x" * prefix + CANARY.encode() + b"\n"
    streams = {"stdout": b"safe stdout\n", "stderr": b"safe stderr\n", stream: payload}
    async with _approved_product(tmp_path, **streams) as case:
        completed = await case.resume()
        assert completed.status is TurnStatus.FAILED
        assert completed.error.code == "public_output_secret_leak"
        result = _result(completed)
        assert result.output is None
        assert result.trusted_action is not None
        assert result.trusted_action.state == "succeeded"
        assert result.trusted_action.artifact_sha256 is None
        assert case.runtime.lease is not None
        document = build_trusted_process_output(
            "unit-tests", case.runtime.lease, streams["stdout"], streams["stderr"]
        )
        preview = trusted_process_public_output(document, include_preview=True)
        assert CANARY not in json.dumps(preview)
        if location == "across-chunks":
            chunks = [chunk.data() for chunk in document.chunks if chunk.stream == stream]
            assert len(chunks) == 2 and all(CANARY.encode() not in chunk for chunk in chunks)
            assert CANARY.encode() in b"".join(chunks)
        assert CANARY not in completed.model_dump_json()
        assert all(CANARY not in request.model_dump_json() for request in case.provider.requests)
        assert case.artifact_count() == 0 and len(case.provider.requests) == 1
        assert case.router.status(case.plan_id).state == "succeeded"
        assert case.runtime.reconcile_calls == 0
        await _assert_no_replay(case, completed)
        for name in ("sessions.db", "plans.db", "audit.db"):
            for suffix in ("", "-wal", "-shm"):
                path = tmp_path / (name + suffix)
                if path.exists():
                    assert CANARY.encode() not in path.read_bytes()


async def test_cancelled_execution_reconciles_to_v2_without_replay(tmp_path: Path) -> None:
    async with _approved_product(tmp_path, cancellable=True) as case:
        execution = asyncio.create_task(case.router.execute(case.plan_id))
        await asyncio.wait_for(cast(_CancellableRuntime, case.runtime).started.wait(), timeout=1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert case.router.status(case.plan_id).state == "unknown"
        reconciled = await case.router.reconcile(case.plan_id)
        assert reconciled.kind == "failed" and reconciled.error_code == "process_cancelled"
        assert isinstance(reconciled.output, dict)
        assert reconciled.output["version"] == "trusted-process-output/v2"
        completed = await case.resume()
        assert completed.status is TurnStatus.COMPLETED
        result = _result(completed)
        assert result.outcome == "failed" and result.error.code == "process_cancelled"
        assert isinstance(result.output, dict)
        assert result.output["version"] == "trusted-process-output/v2"
        assert case.router.status(case.plan_id).state == "failed"
        assert case.runtime.reconcile_calls == 1
        await _read_archive(case, result)
        await _assert_no_replay(case, completed)


@pytest.mark.parametrize("failure", ["deadline", "owner-missing"])
async def test_rebuild_failure_preserves_terminal_fact_without_publication_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async with _approved_product(tmp_path) as case:
        outcome = await case.router.execute(case.plan_id)
        assert outcome.kind == "succeeded"
        events = case.router.events(case.plan_id)
        if failure == "owner-missing":
            case.runtime.lease = None
            error_code = "trusted_action_output_failed"
        else:

            async def blocked_output(*_: Any) -> bytes:
                await asyncio.Future()
                raise AssertionError("阻塞读取只能由真实投影期限取消")

            monkeypatch.setattr(case.runtime, "output", blocked_output)
            monkeypatch.setattr(
                agent_gateway_output,
                "DEFAULT_OUTPUT_BUDGET",
                agent_gateway_output.DEFAULT_OUTPUT_BUDGET.model_copy(
                    update={"timeout_seconds": 0.05}
                ),
            )
            error_code = "trusted_action_output_timeout"
        with pytest.raises(KernelError) as caught:
            await case.project_terminal()
        assert caught.value.code == error_code
        completed = await case.resume()
        assert completed.status is TurnStatus.INTERRUPTED
        assert completed.error.code == "uncertain_effect"
        assert _result(completed).outcome == "unknown" and _result(completed).output is None
        assert case.router.status(case.plan_id).state == "succeeded"
        assert case.router.events(case.plan_id) == events
        assert case.artifact_count() == 0 and len(case.provider.requests) == 1
        assert case.runtime.run_calls == 1 and case.runtime.reconcile_calls == 0
        await _assert_no_replay(case, completed)


async def test_v2_dto_rejects_open_or_untruthful_preview_shapes(tmp_path: Path) -> None:
    async with _approved_product(tmp_path) as case:
        outcome = await case.router.execute(case.plan_id)
        assert isinstance(outcome.output, dict)
        PublicProcessOutputSummaryV2.model_validate(outcome.output)
        invalid_previews = [
            {"text": "x" * 1025, "size_bytes": 1025, "truncated": False},
            {"text": None, "size_bytes": 1, "truncated": True},
            {"text": "\x00", "size_bytes": 1, "truncated": True},
            {"text": "中", "size_bytes": 1, "truncated": True},
            {"text": "safe output\n", "size_bytes": True, "truncated": False},
            {"text": "safe output\n", "size_bytes": "12", "truncated": False},
            {"text": "safe output\n", "size_bytes": 12, "truncated": True},
            {"text": "safe output\n", "size_bytes": 12, "truncated": False, "extra": 1},
        ]
        for preview in invalid_previews:
            public = deepcopy(outcome.output)
            public["diagnostic_preview"]["stdout"] = preview
            with pytest.raises(ValidationError):
                PublicProcessOutputSummaryV2.model_validate(public)
        for field in ("diagnostic_preview", "stderr"):
            public = deepcopy(outcome.output)
            if field == "diagnostic_preview":
                del public[field]
            else:
                del public["diagnostic_preview"][field]
            with pytest.raises(ValidationError):
                PublicProcessOutputSummaryV2.model_validate(public)
        public = deepcopy(outcome.output)
        public["extra"] = "not-closed"
        with pytest.raises(ValidationError):
            PublicProcessOutputSummaryV2.model_validate(public)
        assert case.artifact_count() == 0 and case.runtime.run_calls == 1
