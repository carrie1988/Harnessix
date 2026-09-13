from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import (
    PRODUCT_ACTION_SOURCE,
    WORKSPACE_PATCH_TOOL,
    WorkspacePatchActionExecutor,
    WorkspacePatchTransactionPlanner,
    build_workspace_patch_definition,
    workspace_patch_descriptor,
)
from harnessix.delivery.trusted_action_contracts import (
    MAX_WORKSPACE_PATCH_INPUT_BYTES,
    WorkspaceActionReviewRecord,
    WorkspacePatchFile,
    WorkspacePatchInput,
    parse_workspace_action_review,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_workspace_patch_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.contracts import ActionRouteSnapshot, CodingActionInvocation
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.contracts import WorkspaceLease
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _proposal() -> WorkspacePatchInput:
    return WorkspacePatchInput(
        files=(
            WorkspacePatchFile(
                operation="replace",
                path="src/modified.py",
                expected_sha256=_sha("old\n"),
                content="new\n",
                mode=0o644,
            ),
            WorkspacePatchFile(
                operation="create",
                path="src/新增.py",
                content="print('新增')\n",
                mode=0o644,
            ),
            WorkspacePatchFile(
                operation="delete",
                path="tests/deleted.txt",
                expected_sha256=_sha("remove\n"),
            ),
        )
    )


def _action_step(proposal: WorkspacePatchInput) -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="patch-response"),
        ToolCallCompleted(
            call_id="patch-call",
            tool=WORKSPACE_PATCH_TOOL,
            arguments=proposal.model_dump(mode="json"),
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def _approval(turn: Turn) -> TrustedActionApprovalRequestContent:
    return next(
        item.content
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    )


def _planned_action(
    root: Path,
    parent: Path,
    proposal: WorkspacePatchInput,
) -> tuple[
    TrustedActionRouter,
    WorkspacePatchActionExecutor,
    WorkspacePatchTransactionPlanner,
    SQLiteExecutionPlanStore,
    SQLiteActionAuditStore,
    SQLiteWorkspaceTransactionStore,
    WorkspaceLeaseStore,
    ActionRouteSnapshot,
]:
    environment = build_fixed_product_action_environment(root)
    plans = SQLiteExecutionPlanStore(parent / "plans.db")
    audit = SQLiteActionAuditStore(parent / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(parent / "delivery")
    leases = WorkspaceLeaseStore(parent / "leases.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=environment.workspace_root,
    )
    definition = build_workspace_patch_definition(
        transactions,
        leases,
        environment.workspace_root,
    )
    assert isinstance(definition.executor, WorkspacePatchActionExecutor)
    router.register(definition)
    descriptor = workspace_patch_descriptor()
    route = router.plan(
        CodingActionInvocation(
            invocation_id=uuid4(),
            source="builtin",
            source_id=PRODUCT_ACTION_SOURCE,
            tool=descriptor.name,
            tool_version=descriptor.version,
            tool_fingerprint=tool_fingerprint(descriptor),
            arguments=proposal.model_dump(mode="json"),
            idempotency_key=_sha("planned-action" + str(parent)),
        ),
        environment.context(str(root)),
    )
    planner = WorkspacePatchTransactionPlanner(transactions, environment.workspace_root)
    planner.prepare(route.plan, proposal)
    router.decide(
        route.plan.execution.plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    return (
        router,
        definition.executor,
        planner,
        plans,
        audit,
        transactions,
        leases,
        route,
    )


def test_workspace_patch_input_rejects_ambiguous_operations_and_total_budget() -> None:
    with pytest.raises(ValidationError):
        WorkspacePatchFile(operation="create", path="a.py", expected_sha256="0" * 64)
    with pytest.raises(ValidationError):
        WorkspacePatchFile(
            operation="delete",
            path="a.py",
            expected_sha256="0" * 64,
            content="unexpected",
        )
    with pytest.raises(ValidationError):
        WorkspacePatchInput(
            files=(
                WorkspacePatchFile(operation="create", path="a.py", content="a", mode=0o644),
                WorkspacePatchFile(operation="create", path="a.py", content="b", mode=0o644),
            )
        )
    with pytest.raises(ValidationError):
        WorkspacePatchInput(
            files=(
                WorkspacePatchFile(
                    operation="create",
                    path="large.py",
                    content="界" * (MAX_WORKSPACE_PATCH_INPUT_BYTES // 3 + 1),
                    mode=0o644,
                ),
            )
        )


def test_workspace_patch_public_schemas_match_runtime_contracts() -> None:
    root = Path(__file__).parents[2] / "spec"
    assert json.loads((root / "workspace-patch-input-v1.schema.json").read_text()) == (
        WorkspacePatchInput.model_json_schema()
    )
    assert (
        json.loads((root / "workspace-action-review-record-v1.schema.json").read_text())
        == TypeAdapter(WorkspaceActionReviewRecord).json_schema()
    )


@pytest.mark.skipif(os.name != "posix", reason="安全写端口只在POSIX广告")
async def test_agent_patch_review_approval_delivery_and_artifact_are_one_bound_chain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/modified.py").write_text("old\n", encoding="utf-8")
    (root / "tests/deleted.txt").write_text("remove\n", encoding="utf-8")
    proposal = _proposal()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    confirmation_lost = False

    def lose_first_review_confirmation(point: str) -> None:
        nonlocal confirmation_lost
        if point == "action_review.after_commit" and not confirmation_lost:
            confirmation_lost = True
            raise OSError("模拟Action Review提交确认丢失")

    artifacts = SQLiteArtifactStore(sessions, fault=lose_first_review_confirmation)
    environment = build_fixed_product_action_environment(root)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
    leases = WorkspaceLeaseStore(tmp_path / "leases.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=environment.workspace_root,
    )
    provider = ScriptedProvider([_action_step(proposal), answer("修改完成")])

    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        composition = build_workspace_patch_composition(
            build_product_action_config(),
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=tools.workspace_scope,
        )
        assert composition.gateway is not None
        assert [item.name for item in composition.catalog.definitions()] == [WORKSPACE_PATCH_TOOL]
        async with AgentRuntime(
            sessions,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            trusted_actions=composition.gateway,
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            waiting = await runtime.run_turn(
                thread.thread_id,
                "完成三个文件变更",
                request_id="trusted-patch",
            )
            request = _approval(waiting)
            assert waiting.status is TurnStatus.WAITING_APPROVAL
            assert request.presentation == "patch_batch"
            assert request.diff_artifact is not None and request.diff_artifact.complete
            page = await artifacts.read(
                thread.thread_id,
                tools.workspace_scope,
                request.diff_artifact.artifact_id,
                limit=200,
            )
            assert page.next_offset is None
            call = next(
                item.content for item in waiting.items if isinstance(item.content, ToolCallContent)
            )
            replayed = await artifacts.publish_action_review(
                thread.thread_id,
                waiting.turn_id,
                call,
                page.text.encode(),
                artifact_id=request.diff_artifact.artifact_id,
                workspace_scope=tools.workspace_scope,
                expected_sequence=(await sessions.get_thread(thread.thread_id)).sequence,
            )
            assert replayed == request.diff_artifact
            with pytest.raises(KernelError) as conflict:
                await artifacts.publish_action_review(
                    thread.thread_id,
                    waiting.turn_id,
                    call,
                    b'{"different":true}\n',
                    artifact_id=request.diff_artifact.artifact_id,
                    workspace_scope=tools.workspace_scope,
                    expected_sequence=(await sessions.get_thread(thread.thread_id)).sequence,
                )
            assert conflict.value.code == "artifact_conflict"
            review = parse_workspace_action_review(page.text.encode())
            assert review.summary.file_count == 3
            assert {item.entry.kind for item in review.entries} == {
                "added",
                "modified",
                "deleted",
            }
            assert "新增.py" in "".join(item.text for item in review.chunks)

            await runtime.reply_approval(
                thread.thread_id,
                waiting.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="reviewer",
                ),
            )
            completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)

    result = next(
        item.content for item in completed.items if isinstance(item.content, ToolResultContent)
    )
    assert completed.status is TurnStatus.COMPLETED
    assert confirmation_lost
    assert result.outcome == "succeeded"
    assert result.trusted_action is not None
    assert result.trusted_action.artifact_sha256 == request.diff_artifact.sha256
    assert result.diff_artifact == request.diff_artifact
    assert (root / "src/modified.py").read_text(encoding="utf-8") == "new\n"
    assert (root / "src/新增.py").read_text(encoding="utf-8") == "print('新增')\n"
    assert not (root / "tests/deleted.txt").exists()
    record = transactions.load(request.plan_id)
    assert record.state == "published"
    assert record.plan.source == router.status(request.plan_id).plan.execution.workspace
    assert json.loads(page.text.splitlines()[0])["plan_fingerprint"] == record.plan.fingerprint
    transactions.close()
    leases.close()
    plans.close()
    audit.close()


@pytest.mark.skipif(os.name != "posix", reason="安全写端口只在POSIX广告")
async def test_review_committed_before_session_approval_is_not_readable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "app.py").write_text("old\n", encoding="utf-8")
    proposal = WorkspacePatchInput(
        files=(
            WorkspacePatchFile(
                operation="replace",
                path="app.py",
                expected_sha256=_sha("old\n"),
                content="new\n",
                mode=0o644,
            ),
        )
    )
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    environment = build_fixed_product_action_environment(root)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
    leases = WorkspaceLeaseStore(tmp_path / "leases.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=environment.workspace_root,
    )
    injected = False

    def stop_after_review(point: str) -> None:
        nonlocal injected
        if point == "runtime.after_trusted_action_prepare" and not injected:
            injected = True
            raise RuntimeError("模拟Review提交后Session提交前退出")

    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        composition = build_workspace_patch_composition(
            build_product_action_config(),
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=tools.workspace_scope,
        )
        assert composition.gateway is not None
        async with AgentRuntime(
            sessions,
            ScriptedProvider([_action_step(proposal)]),
            scoped_tools=tools,
            artifacts=artifacts,
            trusted_actions=composition.gateway,
            fault=stop_after_review,
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            failed = await runtime.run_turn(
                thread.thread_id,
                "修改文件",
                request_id="orphan-review",
            )
            assert failed.status is TurnStatus.INTERRUPTED
            with sqlite3.connect(sessions.path) as database:
                row = database.execute(
                    "SELECT manifest_json FROM agent_artifacts WHERE purpose='action_review'"
                ).fetchone()
            assert row is not None
            orphan = ArtifactRef.model_validate_json(row[0])
            with pytest.raises(KernelError) as hidden:
                await artifacts.read(
                    thread.thread_id,
                    tools.workspace_scope,
                    orphan.artifact_id,
                )
            assert hidden.value.code == "artifact_not_found"
            assert (root / "app.py").read_text(encoding="utf-8") == "old\n"
    transactions.close()
    leases.close()
    plans.close()
    audit.close()


def test_windows_or_disabled_patch_is_omitted_without_router_binding(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    environment = build_fixed_product_action_environment(root)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
    leases = WorkspaceLeaseStore(tmp_path / "leases.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=environment.workspace_root,
    )

    composition = build_workspace_patch_composition(
        build_product_action_config(workspace_patch_enabled=False),
        environment,
        router,
        transactions,
        leases,
        artifacts,
        artifact_workspace_scope="0" * 64,
    )

    assert composition.gateway is None
    assert composition.report.capabilities[0].status == "omitted"
    assert composition.report.capabilities[0].reason_code == "disabled"
    assert router.bindings() == ()
    transactions.close()
    leases.close()
    plans.close()
    audit.close()


@pytest.mark.skipif(os.name != "posix", reason="安全写端口只在POSIX广告")
async def test_workspace_lease_contention_never_starts_effect_and_reconciles_failed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    proposal = WorkspacePatchInput(
        files=(
            WorkspacePatchFile(
                operation="create",
                path="created.txt",
                content="content\n",
                mode=0o644,
            ),
        )
    )
    router, _, planner, plans, audit, transactions, leases, route = _planned_action(
        root, tmp_path, proposal
    )
    record = planner.load(route.plan, proposal)
    competing = leases.acquire(record.plan.source.workspace_id, "other-owner", ttl_seconds=60)

    outcome = await router.execute(record.transaction_id)

    assert outcome.kind == "unknown"
    assert transactions.load(record.transaction_id).state == "prepared"
    assert not (root / "created.txt").exists()
    leases.release(competing)
    reconciled = await router.reconcile(record.transaction_id)
    assert reconciled.kind == "failed"
    assert reconciled.error_code == "delivery_not_applied"
    assert router.status(record.transaction_id).state == "failed"
    transactions.close()
    leases.close()
    plans.close()
    audit.close()


@pytest.mark.skipif(os.name != "posix", reason="安全写端口只在POSIX广告")
async def test_cancel_between_members_keeps_prefix_and_reconcile_never_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    proposal = WorkspacePatchInput(
        files=tuple(
            WorkspacePatchFile(
                operation="create",
                path=path,
                content=f"{path}\n",
                mode=0o644,
            )
            for path in ("a.txt", "b.txt")
        )
    )
    router, executor, planner, plans, audit, transactions, leases, route = _planned_action(
        root, tmp_path, proposal
    )
    record = planner.load(route.plan, proposal)
    original = executor._runtime.publish_next

    def cancel_after_first(
        transaction_id: UUID,
        workspace_root: str | Path,
        *,
        approval_fingerprint: str,
        lease: WorkspaceLease,
    ) -> WorkspaceTransactionRecord:
        current = original(
            transaction_id,
            workspace_root,
            approval_fingerprint=approval_fingerprint,
            lease=lease,
        )
        if current.cursor == 1:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
        return current

    monkeypatch.setattr(executor._runtime, "publish_next", cancel_after_first)
    with pytest.raises(asyncio.CancelledError):
        await router.execute(record.transaction_id)

    interrupted = transactions.load(record.transaction_id)
    assert interrupted.state == "publishing" and interrupted.cursor == 1
    assert (root / "a.txt").read_text(encoding="utf-8") == "a.txt\n"
    assert not (root / "b.txt").exists()
    reconciled = await router.reconcile(record.transaction_id)
    assert reconciled.kind == "manual_intervention"
    assert reconciled.error_code == "delivery_partial_effect"
    assert not (root / "b.txt").exists()
    assert router.status(record.transaction_id).state == "manual_intervention"
    transactions.close()
    leases.close()
    plans.close()
    audit.close()


@pytest.mark.skipif(os.name != "posix", reason="安全写端口只在POSIX广告")
async def test_post_approval_source_drift_fails_before_router_claim_or_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    proposal = WorkspacePatchInput(
        files=(
            WorkspacePatchFile(
                operation="replace",
                path="target.txt",
                expected_sha256=_sha("before\n"),
                content="approved\n",
                mode=0o644,
            ),
        )
    )
    router, _, planner, plans, audit, transactions, leases, route = _planned_action(
        root, tmp_path, proposal
    )
    record = planner.load(route.plan, proposal)
    target.write_text("external\n", encoding="utf-8")

    with pytest.raises(KernelError) as stale:
        await router.execute(record.transaction_id)

    assert stale.value.code == "execution_plan_stale"
    assert router.status(record.transaction_id).state == "ready"
    assert transactions.load(record.transaction_id).state == "prepared"
    assert target.read_text(encoding="utf-8") == "external\n"
    transactions.close()
    leases.close()
    plans.close()
    audit.close()
