from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.delivery.git import GitDeliveryRuntime
from harnessix.delivery.git_contracts import (
    GitPushActionInput,
    GitPushIntent,
    git_push_intent_digest,
)
from harnessix.delivery.git_push import (
    ApprovedGitPushPolicy,
    GitPushActionExecutor,
    GitPushRoutedExecutor,
    _decode_single_line,
    git_push_tool_definition,
    git_remote_url_digest,
)
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import (
    ActionContext,
    ActionRequest,
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    Principal,
    RiskLevel,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.runtime import ActionService
from harnessix.storage.sqlite_journal import SQLiteEffectJournal
from harnessix.trusted_actions.contracts import (
    CodingActionInvocation,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.delivery.test_git import _git, _run
from tests.trusted_actions.test_router import context

_COMMIT_TIME = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def _push_intent(
    *,
    repository_digest: str,
    remote: Path,
    local_oid: str,
    suffix: str,
) -> GitPushIntent:
    candidate = GitPushIntent.model_construct(
        _fields_set=None,
        push_id=uuid4(),
        repository_binding_digest=repository_digest,
        remote_name="origin",
        remote_url_sha256=git_remote_url_digest(str(remote), allow_file=True),
        local_ref=f"refs/heads/harnessix/{suffix}",
        local_oid=local_oid,
        remote_ref=f"refs/heads/harnessix/{suffix}",
        expected_remote_oid=None,
        force_mode="fast_forward_only",
        idempotency_key=f"git-push:{suffix}:{local_oid}",
        digest="0" * 64,
    )
    return GitPushIntent(
        **candidate.model_dump(exclude={"digest"}),
        digest=git_push_intent_digest(candidate),
    )


def _git_fixture(tmp_path: Path, *, suffix: str):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(remote, "init", "--bare", "-q")
    repository = tmp_path / "repository"
    repository.mkdir()
    _run(repository, "init", "-q")
    _run(repository, "config", "user.name", "Harnessix Test")
    _run(repository, "config", "user.email", "test@harnessix.invalid")
    _run(repository, "config", "core.autocrlf", "false")
    _run(repository, "remote", "add", "origin", str(remote))
    (repository / "file.txt").write_bytes(b"before\n")
    _run(repository, "add", "--", "file.txt")
    _run(repository, "commit", "-qm", "baseline")
    prepared = prepare_workspace_transaction(
        repository,
        {"file.txt": DesiredWorkspaceFile(b"after\n", 0o644)},
        request_id=f"push-{suffix}",
    )
    workspace_store = SQLiteWorkspaceTransactionStore(tmp_path / "state/workspace")
    git_store = SQLiteGitDeliveryStore(tmp_path / "state/git")
    leases = WorkspaceLeaseStore(tmp_path / "state/leases.db")
    delivery = GitDeliveryRuntime(workspace_store, git_store, leases, _git())
    transaction = workspace_store.save(prepared)
    lease = leases.acquire(transaction.plan.source.workspace_id, "test", ttl_seconds=60)
    worktree = delivery.plan_worktree(transaction.transaction_id, repository)
    ready = delivery.create_worktree(
        worktree.worktree_id,
        repository,
        approval_fingerprint=transaction.plan.fingerprint,
        lease=lease,
    )
    checkpoint = delivery.create_checkpoint(ready.worktree_id, repository, lease=lease)
    commit = delivery.plan_commit(
        checkpoint.checkpoint_id,
        repository,
        branch=f"harnessix/{suffix}",
        author_name="Harnessix Agent",
        author_email="agent@harnessix.invalid",
        message="Push transaction",
        authored_at=_COMMIT_TIME,
    )
    committed = delivery.commit(
        commit.commit_id,
        repository,
        approval_fingerprint=commit.spec.fingerprint,
        lease=lease,
    )
    assert committed.commit_oid is not None
    intent = _push_intent(
        repository_digest=ready.plan.repository.digest,
        remote=remote,
        local_oid=committed.commit_oid,
        suffix=suffix,
    )
    return (
        repository,
        remote,
        workspace_store,
        git_store,
        leases,
        delivery,
        ready.plan.repository,
        intent,
    )


async def _route(
    tmp_path: Path,
    *,
    repository: Path,
    delivery: GitDeliveryRuntime,
    repository_binding,
    intent: GitPushIntent,
    fault=lambda _: None,
):
    plans = SQLiteExecutionPlanStore(tmp_path / "state/action-plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/action-audit.db")
    push = GitPushActionExecutor(
        delivery=delivery,
        repository_root=repository,
        repository=repository_binding,
        git_executable=_git(),
        state_root=tmp_path / "state/push-git",
        allowed_protocols=(),
        allow_file_remote=True,
        fault=fault,
    )
    intent = push.prepare_intent(
        remote_name=intent.remote_name,
        local_ref=intent.local_ref,
        remote_ref=intent.remote_ref,
        expected_remote_oid=intent.expected_remote_oid,
        force_mode=intent.force_mode,
        idempotency_key=intent.idempotency_key,
        push_id=intent.push_id,
    )
    action_definition = git_push_tool_definition(push)
    registry = ToolRegistry()
    registry.register(action_definition)
    service = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "state/effects.db"),
        registry=registry,
        policy_engine=ApprovedGitPushPolicy(plans=plans, audit=audit),
    )
    await service.initialize()
    routed = GitPushRoutedExecutor(
        actions=service,
        principal=Principal(tenant_id="local", subject_id="test", framework="harnessix-code"),
    )
    descriptor = action_definition.descriptor()
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix",
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(GitPushIntent.model_json_schema()),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        recovery_mode="external_reconcile",
        executor_id="git.push.action-plane",
    )

    def resolve(arguments, _):  # type: ignore[no-untyped-def]
        checked = GitPushIntent.model_validate_json(arguments.model_dump_json())
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="external",
                    access="write",
                    identifier={
                        "remote": checked.remote_url_sha256,
                        "ref": checked.remote_ref,
                    },
                ),
                canonical_action_resource(
                    kind="git_ref",
                    access="update",
                    identifier={"remote": checked.remote_name, "ref": checked.remote_ref},
                    attributes={"expected": checked.expected_remote_oid},
                ),
            )
        )

    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: repository,
    )
    router.register(TrustedActionDefinition(binding, GitPushIntent, resolve, routed))
    invocation = CodingActionInvocation(
        source="builtin",
        source_id="harnessix",
        tool=binding.tool,
        tool_version=binding.tool_version,
        tool_fingerprint=binding.tool_fingerprint,
        arguments=intent.model_dump(mode="json"),
        idempotency_key=intent.idempotency_key,
    )
    planned = router.plan(invocation, context(repository))
    return router, planned, plans, audit, service


async def test_git_push_requires_route_approval_and_updates_one_remote_ref(
    tmp_path: Path,
) -> None:
    values = _git_fixture(tmp_path, suffix="approved")
    repository, remote, workspace_store, git_store, leases, delivery, binding, intent = values
    router, planned, plans, audit, service = await _route(
        tmp_path,
        repository=repository,
        delivery=delivery,
        repository_binding=binding,
        intent=intent,
    )
    try:
        assert planned.state == "pending_approval"
        router.decide(
            planned.plan.execution.plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
        )
        outcome = await router.execute(planned.plan.execution.plan_id)
        remote_oid = _run(remote, "rev-parse", intent.remote_ref).decode().strip()
        action = await service.get(planned.plan.external_action_id)
        assert outcome.kind == "succeeded"
        assert remote_oid == intent.local_oid
        assert action.status.value == "succeeded"
        assert [event.to_state for event in router.events(planned.plan.execution.plan_id)] == [
            "pending_approval",
            "ready",
            "running",
            "succeeded",
        ]
    finally:
        await service.close()
        audit.close()
        plans.close()
        leases.close()
        git_store.close()
        workspace_store.close()


async def test_push_response_loss_reconciles_without_second_push(tmp_path: Path) -> None:
    values = _git_fixture(tmp_path, suffix="uncertain")
    repository, remote, workspace_store, git_store, leases, delivery, binding, intent = values
    calls = 0

    def lose_response(point: str) -> None:
        nonlocal calls
        if point == "git_push.after_command":
            calls += 1
            raise UncertainEffectError("response lost")

    router, planned, plans, audit, service = await _route(
        tmp_path,
        repository=repository,
        delivery=delivery,
        repository_binding=binding,
        intent=intent,
        fault=lose_response,
    )
    try:
        router.decide(
            planned.plan.execution.plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
        )
        unknown = await router.execute(planned.plan.execution.plan_id)
        assert unknown.kind == "unknown"
        assert _run(remote, "rev-parse", intent.remote_ref).decode().strip() == intent.local_oid

        reconciled = await router.reconcile(planned.plan.execution.plan_id)
        assert reconciled.kind == "succeeded"
        assert calls == 1
        assert [event.to_state for event in router.events(planned.plan.execution.plan_id)][-3:] == [
            "unknown",
            "reconciling",
            "succeeded",
        ]
    finally:
        await service.close()
        audit.close()
        plans.close()
        leases.close()
        git_store.close()
        workspace_store.close()


async def test_direct_action_service_push_bypass_is_denied(tmp_path: Path) -> None:
    values = _git_fixture(tmp_path, suffix="bypass")
    repository, remote, workspace_store, git_store, leases, delivery, binding, intent = values
    router, planned, plans, audit, service = await _route(
        tmp_path,
        repository=repository,
        delivery=delivery,
        repository_binding=binding,
        intent=intent,
    )
    try:
        assert planned.plan.external_action_id is not None
        payload = GitPushActionInput(
            route_plan_id=planned.plan.execution.plan_id,
            route_plan_fingerprint=planned.plan.fingerprint,
            external_action_id=planned.plan.external_action_id,
            intent=intent,
        )
        denied = await service.submit(
            ActionRequest(
                action_id=planned.plan.external_action_id,
                tool="git.push",
                arguments=payload.model_dump(mode="json"),
                principal=Principal(
                    tenant_id="local", subject_id="attacker", framework="direct-call"
                ),
                context=ActionContext(session_id="bypass", run_id="bypass"),
                effect_hint=EffectClass.NON_IDEMPOTENT_WRITE,
                idempotency_key=intent.idempotency_key,
            )
        )
        assert denied.status.value == "denied"
        assert router.status(planned.plan.execution.plan_id).state == "pending_approval"
        assert _run(repository, "ls-remote", "--refs", "origin", intent.remote_ref) == b""
    finally:
        await service.close()
        audit.close()
        plans.close()
        leases.close()
        git_store.close()
        workspace_store.close()


async def test_remote_configuration_drift_after_approval_fails_closed(tmp_path: Path) -> None:
    values = _git_fixture(tmp_path, suffix="remote-drift")
    repository, remote, workspace_store, git_store, leases, delivery, binding, intent = values
    router, planned, plans, audit, service = await _route(
        tmp_path,
        repository=repository,
        delivery=delivery,
        repository_binding=binding,
        intent=intent,
    )
    changed_remote = tmp_path / "changed.git"
    changed_remote.mkdir()
    _run(changed_remote, "init", "--bare", "-q")
    try:
        router.decide(
            planned.plan.execution.plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
        )
        _run(repository, "remote", "set-url", "origin", str(changed_remote))
        outcome = await router.execute(planned.plan.execution.plan_id)

        assert outcome.kind == "failed"
        assert outcome.error_code == "git_repository_changed"
        assert router.status(planned.plan.execution.plan_id).state == "failed"
        assert _run(repository, "ls-remote", "--refs", str(remote)) == b""
        assert _run(repository, "ls-remote", "--refs", str(changed_remote)) == b""
    finally:
        await service.close()
        audit.close()
        plans.close()
        leases.close()
        git_store.close()
        workspace_store.close()


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/repository.git",
        "https://user:password@example.com/repository.git",
        "https://example.com/team/../repository.git",
        "ssh://example.com/",
        "file:///definitely/not/a/harnessix/repository.git",
    ],
)
def test_remote_url_rejects_protocol_credentials_and_ambiguous_paths(value: str) -> None:
    with pytest.raises(KernelError):
        git_remote_url_digest(value)


def test_remote_url_has_stable_host_and_path_canonicalization() -> None:
    assert git_remote_url_digest("https://EXAMPLE.com/team/%7Erepo.git") == (
        git_remote_url_digest("https://example.com/team/~repo.git")
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://example .com/repository.git",
        "ssh://user%40evil@example.com/repository.git",
        "https://example.com/repository.git\x7f",
    ],
)
def test_remote_url_rejects_invalid_host_user_and_control_character(value: str) -> None:
    with pytest.raises(KernelError):
        git_remote_url_digest(value)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_git_single_line_decoder_accepts_platform_line_endings(ending: bytes) -> None:
    assert (
        _decode_single_line(
            b"value" + ending,
            encoding="ascii",
            error_code="test_error",
            error_message="invalid",
        )
        == "value"
    )


@pytest.mark.parametrize("value", [b"", b"one\rtwo", b"one\ntwo\n", b"\xff"])
def test_git_single_line_decoder_rejects_empty_multiline_and_invalid_encoding(
    value: bytes,
) -> None:
    with pytest.raises(KernelError) as invalid:
        _decode_single_line(
            value,
            encoding="ascii",
            error_code="test_error",
            error_message="invalid",
        )
    assert invalid.value.code == "test_error"


@pytest.mark.parametrize(
    ("remote_name", "local_ref", "remote_ref"),
    [
        ("--all", "refs/heads/main", "refs/heads/main"),
        ("origin", "--help", "refs/heads/main"),
        ("origin", "refs/heads/main", "refs/heads/.hidden"),
        ("origin", "refs/heads/main", "refs/heads/team//main"),
    ],
)
def test_prepare_intent_rejects_unsafe_command_fields_before_git_runs(
    remote_name: str,
    local_ref: str,
    remote_ref: str,
) -> None:
    executor = object.__new__(GitPushActionExecutor)
    executor._verify_repository = lambda: pytest.fail("Git must not run")  # type: ignore[method-assign]
    with pytest.raises(KernelError) as invalid:
        executor.prepare_intent(
            remote_name=remote_name,
            local_ref=local_ref,
            remote_ref=remote_ref,
            expected_remote_oid=None,
            force_mode="fast_forward_only",
            idempotency_key="key",
        )
    assert invalid.value.code == "git_push_intent_invalid"
