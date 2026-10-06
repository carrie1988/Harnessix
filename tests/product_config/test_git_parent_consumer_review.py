"""Git 父历史消费定向回归：只使用临时原账本与原生 Patch，不调用模型或 Git。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Item,
    ItemStatus,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionEffect,
    Turn,
    TurnStatus,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import (
    PRODUCT_ACTION_SOURCE,
    WorkspacePatchTransactionPlanner,
    build_workspace_patch_definition,
)
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config import git_baseline as baseline_module
from harnessix.product_config import git_delivery_source as source_module
from harnessix.product_config.action_composition import build_fixed_product_action_environment
from harnessix.product_config.git_baseline_contracts import (
    GitBaselineMember,
    ProductGitDeliveryBaseline,
    product_git_baseline_digest,
)
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.workspace_patch_source import load_owned_workspace_patch
from harnessix.product_config.workspace_patch_source_contracts import ProductGitDeliverySource
from harnessix.trusted_actions.contracts import CodingActionInvocation
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


def _forbidden(*_args, **_kwargs):
    raise AssertionError("此定向回归不得启动子进程或进入 Git 查询")


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _forbidden)


class _History:
    """构造真实持久 Route/Transaction；Thread 为受信调用边界的合法领域输入。"""

    def __init__(self, parent, stack, *, constructor_checkpoint=None):
        self.root = parent / "workspace"
        (self.root / "a/b").mkdir(parents=True)
        (self.root / "a/b/file.py").write_bytes(b"old\n")
        self.transactions = SQLiteWorkspaceTransactionStore(
            parent / "delivery", checkpoint=constructor_checkpoint
        )
        stack.callback(self.transactions.close)
        self.ports = WorkspaceSnapshotPorts(self.transactions.put_blob, self.transactions.blob)
        self.plans = SQLiteExecutionPlanStore(parent / "plans.db", read_blob=self.transactions.blob)
        stack.callback(self.plans.close)
        self.audit = SQLiteActionAuditStore(
            parent / "audit.db",
            read_blob=self.transactions.blob,
            checkpoint=constructor_checkpoint,
        )
        stack.callback(self.audit.close)
        self.leases = WorkspaceLeaseStore(parent / "leases.db")
        stack.callback(self.leases.close)
        self.environment = build_fixed_product_action_environment(self.root)
        self.router = TrustedActionRouter(
            plans=self.plans, audit=self.audit, workspace_root=self.environment.workspace_root
        )
        definition = build_workspace_patch_definition(
            self.transactions, self.leases, self.environment.workspace_root
        )
        self.router.register(definition)
        self.binding = definition.binding
        now = utc_now()
        self.thread = Thread(
            thread_id=uuid4(), workspace=str(self.root), created_at=now, updated_at=now
        )

    async def publish(self, after, *, parent_history):
        before = (self.root / "a/b/file.py").read_bytes()
        proposal = WorkspacePatchInput(
            files=(
                WorkspacePatchFile(
                    operation="replace",
                    path="a/b/file.py",
                    expected_sha256=hashlib.sha256(before).hexdigest(),
                    content=after.decode(),
                    mode=0o644,
                ),
            )
        )
        turn_id, now = uuid4(), utc_now()
        call = ToolCallContent(
            call_id=uuid4(),
            provider_call_id="offline-review",
            tool=self.binding.tool,
            tool_version=self.binding.tool_version,
            effect_class=self.binding.effect_class,
            arguments=proposal.model_dump(mode="json"),
            requires_approval=True,
            tool_fingerprint=self.binding.tool_fingerprint,
        )
        target = trusted_action_invocation_id(self.thread.thread_id, turn_id, call)
        ports = self.ports if parent_history else None
        self.router._snapshot_ports = ports
        route = self.router.plan(
            CodingActionInvocation(
                invocation_id=target,
                source="builtin",
                source_id=PRODUCT_ACTION_SOURCE,
                tool=call.tool,
                tool_version=call.tool_version,
                tool_fingerprint=call.tool_fingerprint,
                arguments=call.arguments,
                idempotency_key=hashlib.sha256(str(target).encode()).hexdigest(),
            ),
            replace(self.environment.context(str(self.root)), snapshot_ports=ports),
        )
        WorkspacePatchTransactionPlanner(
            self.transactions, self.environment.workspace_root
        ).prepare(route.plan, proposal)
        self.router.decide(
            target, ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="review")
        )
        await self.router.execute(target)
        assert self.router.status(target).state == "succeeded"
        assert self.transactions.load(target).state == "published"
        result = ToolResultContent(
            call_id=call.call_id,
            action_id=target,
            outcome="succeeded",
            trusted_action=TrustedActionEffect(
                plan_id=target,
                plan_fingerprint=route.plan.fingerprint,
                state="succeeded",
                origin="execution",
            ),
        )
        turn = Turn(
            turn_id=turn_id,
            request_id=str(target),
            request_fingerprint="0" * 64,
            status=TurnStatus.COMPLETED,
            budget=Budget(),
            created_at=now,
            completed_at=now,
            items=tuple(
                Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)
                for content in (call, result)
            ),
        )
        self.thread = self.thread.model_copy(update={"turns": (*self.thread.turns, turn)})
        return target

    def collect(self, targets, *, checkpoint=lambda: None, ports=None):
        return source_module.collect_git_delivery_source(
            self.thread,
            targets,
            self.router,
            self.transactions,
            checkpoint=checkpoint,
            snapshot_ports=ports or self.ports,
        )

    def isolate_reader(self, monkeypatch, origin, read):
        if origin == "route":
            monkeypatch.setattr(self.audit, "_read_blob", read)
        else:
            # Route 仍经完整原 Reader，只把故障定位到 Transaction 的首次 Plan 回读。
            original = self.transactions._read_blob
            monkeypatch.setattr(self.audit, "_read_blob", original)
            monkeypatch.setattr(self.transactions, "_read_blob", read)


@pytest.fixture
async def history(tmp_path):
    with ExitStack() as stack:
        yield _History(tmp_path, stack)


@pytest.mark.parametrize("origin", ["route", "transaction"])
@pytest.mark.parametrize("control", ["cancel", "deadline"])
async def test_baseline_history_reader_stops_after_first_controlled_cas_read(
    history, monkeypatch, origin, control
):
    target = await history.publish(b"new\n", parent_history=True)
    token, clock, reads = CancelToken(), [0.0], []
    original = history.transactions._read_blob
    monkeypatch.setattr(baseline_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def read(digest):
        body = original(digest)
        reads.append(digest)
        if len(reads) == 1:
            if control == "cancel":
                token.cancel()
            else:
                clock[0] = 60.0
        return body

    history.isolate_reader(monkeypatch, origin, read)
    expected = TurnCancelled if control == "cancel" else KernelError
    with pytest.raises(expected) as caught:
        await baseline_module.collect_product_git_baseline(
            history.thread,
            (target,),
            history.router,
            history.transactions,
            SimpleNamespace(contract=_forbidden),
            cancel=token,
            snapshot_ports=history.ports,
        )
    if control == "deadline":
        assert caught.value.code == "git_baseline_timeout"
    assert len(reads) == 1, f"调用方已停止，历史 Reader 仍完成 {len(reads)} 次 CAS 回读"


@pytest.mark.parametrize("consumer", ["source", "baseline"])
@pytest.mark.parametrize("origin", ["route", "transaction"])
@pytest.mark.parametrize(
    "code",
    [
        None,
        "workspace_closure_corrupt",
        "delivery_blob_corrupt",
        "delivery_blob_invalid",
        "delivery_store_corrupt",
        "action_audit_store_corrupt",
        "workspace_patch_source_not_owned",
        "workspace_patch_source_not_published",
    ],
)
async def test_history_reader_preserves_upstream_control_identity_at_first_cas_boundary(
    history, monkeypatch, consumer, origin, code
):
    target = await history.publish(b"new\n", parent_history=True)
    reads, stopped = [], [False]
    error = RuntimeError("upstream-control") if code is None else KernelError(code, "上游控制")
    original = history.transactions._read_blob

    def checkpoint():
        if stopped[0]:
            raise error

    def read(digest):
        body = original(digest)
        reads.append(digest)
        stopped[0] = True
        return body

    class ControlledToken(CancelToken):
        def checkpoint(self):
            super().checkpoint()
            checkpoint()

    history.isolate_reader(monkeypatch, origin, read)
    with pytest.raises(type(error)) as caught:
        if consumer == "source":
            history.collect((target,), checkpoint=checkpoint)
        else:
            await baseline_module.collect_product_git_baseline(
                history.thread,
                (target,),
                history.router,
                history.transactions,
                SimpleNamespace(contract=_forbidden),
                cancel=ControlledToken(),
                snapshot_ports=history.ports,
            )
    assert caught.value is error
    assert len(reads) == 1, f"上游已停止，历史 Reader 仍完成 {len(reads)} 次 CAS 回读"


@pytest.mark.parametrize("origin", ["route", "transaction"])
@pytest.mark.parametrize("code", ["workspace_closure_corrupt", "delivery_store_corrupt"])
async def test_per_call_checkpoint_keeps_constructor_control_identity(
    tmp_path, monkeypatch, origin, code
):
    stopped, reads = [False], []
    error = KernelError(code, "原宿主控制")

    def constructor_checkpoint():
        if stopped[0]:
            raise error

    with ExitStack() as stack:
        history = _History(tmp_path, stack, constructor_checkpoint=constructor_checkpoint)
        target = await history.publish(b"new\n", parent_history=True)
        original = history.transactions._read_blob

        def read(digest):
            body = original(digest)
            reads.append(digest)
            stopped[0] = True
            return body

        history.isolate_reader(monkeypatch, origin, read)
        with pytest.raises(KernelError) as caught:
            history.collect((target,), checkpoint=lambda: None)
        assert caught.value is error
        assert len(reads) == 1


@pytest.mark.parametrize("generations", [(False, False), (False, True), (True, False)])
async def test_actual_generation_full_digest_and_strict_inherited_contracts(
    history, monkeypatch, generations
):
    first = await history.publish(b"middle\n", parent_history=generations[0])
    last = await history.publish(b"final\n", parent_history=generations[1])
    writes, write = [], history.ports.write_blob
    ports = WorkspaceSnapshotPorts(
        lambda digest, body: (writes.append(digest), write(digest, body)), history.ports.read_blob
    )
    changes = tuple(
        store._db.total_changes for store in (history.transactions, history.plans, history.audit)
    )
    source = history.collect((last, first), ports=ports)
    new = any(generations)
    source_type = ProductGitDeliverySourceV2 if new else ProductGitDeliverySource
    baseline_type = ProductGitDeliveryBaselineV2 if new else ProductGitDeliveryBaseline
    assert type(source) is source_type
    assert [item.transaction_id for item in source.patches] == [first, last]
    assert bool(writes) is new
    assert source_type.model_validate_json(source.model_dump_json(), strict=True) == source
    body = source.model_dump(mode="json", exclude={"digest"})
    assert source.digest == canonical_digest(body)
    assert ("parent_closure" in body["workspace"]) is new
    candidate = baseline_type.model_construct(
        source=source,
        head_oid="a" * 40,
        head_tree_oid="b" * 40,
        head_ref="refs/heads/main",
        index_observation_sha256="1" * 64,
        index_observation_bytes=0,
        status_sha256="2" * 64,
        config_names_sha256="3" * 64,
        reader_binding="4" * 64,
        members=(GitBaselineMember(path="a/b/file.py", oid="c" * 40, mode="100644"),),
        digest="0" * 64,
    )
    baseline = baseline_type(
        **candidate.model_dump(exclude={"digest"}), digest=product_git_baseline_digest(candidate)
    )
    assert baseline_type.model_validate_json(baseline.model_dump_json(), strict=True) == baseline
    assert baseline.digest == canonical_digest(baseline.model_dump(mode="json", exclude={"digest"}))
    assert baseline.model_dump(mode="json")["source"] == source.model_dump(mode="json")
    assert (
        tuple(
            store._db.total_changes
            for store in (history.transactions, history.plans, history.audit)
        )
        == changes
    )
    invalid = source.model_dump(mode="json")
    invalid["mutations"][0]["after"]["size"] = 1
    invalid["digest"] = canonical_digest({k: v for k, v in invalid.items() if k != "digest"})
    with pytest.raises(ValidationError):
        source_type.model_validate_json(json.dumps(invalid), strict=True)
    if new:
        with pytest.raises(ValidationError):
            ProductGitDeliverySource.model_validate_json(source.model_dump_json(), strict=True)
        with pytest.raises(ValidationError):
            ProductGitDeliveryBaseline.model_validate_json(baseline.model_dump_json(), strict=True)


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
async def test_original_parent_history_error_is_not_recaptured(history, monkeypatch, fault):
    target = await history.publish(b"new\n", parent_history=True)
    historical = history.transactions.load(target).plan.source.parent_closure.sha256
    original, writes = history.transactions._read_blob, []

    def read(digest):
        if digest == historical:
            if fault == "missing":
                raise KernelError("delivery_blob_missing", "Blob不存在")
            return b"corrupt"
        return original(digest)

    history.isolate_reader(monkeypatch, "route", read)
    ports = WorkspaceSnapshotPorts(lambda digest, body: writes.append(digest), original)
    with pytest.raises(KernelError) as caught:
        history.collect((target,), ports=ports)
    assert caught.value.code == (
        "delivery_blob_missing" if fault == "missing" else "action_audit_store_corrupt"
    )
    assert writes == []
    assert (history.root / "a/b/file.py").read_bytes() == b"new\n"


async def test_full_uuid_ownership_preflight_is_before_original_cas(history, monkeypatch):
    target = await history.publish(b"new\n", parent_history=True)
    monkeypatch.setattr(history.transactions, "_read_blob", _forbidden)
    with pytest.raises(KernelError) as caught:
        history.collect((target, uuid4()))
    assert caught.value.code == "git_delivery_source_not_owned"


async def test_mixed_generation_net_zero_still_observes_final_content(history, monkeypatch):
    first = await history.publish(b"new\n", parent_history=False)
    last = await history.publish(b"old\n", parent_history=True)
    reads, original = [], source_module._read_existing

    def read(*args, **kwargs):
        reads.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(source_module, "_read_existing", read)
    with pytest.raises(KernelError) as caught:
        history.collect((first, last))
    assert caught.value.code == "git_delivery_source_no_change"
    assert reads == ["a/b/file.py"]
    (history.root / "a/b/file.py").write_bytes(b"third\n")
    with pytest.raises(KernelError) as caught:
        history.collect((first, last))
    assert caught.value.code == "git_delivery_source_changed"


async def test_parent_generation_retains_original_member_and_image_limits(history):
    target = await history.publish(b"new\n", parent_history=True)
    owned = load_owned_workspace_patch(history.thread, target, history.router, history.transactions)
    original = owned.record.plan.mutations[0]
    # 仅容量护栏负对照；合成元数据不代表授权来源或实际大文件验收。
    cases = (
        tuple(original.model_copy(update={"path": f"file-{i}.py"}) for i in range(256)),
        tuple(
            original.model_copy(
                update={
                    "path": f"file-{i}.py",
                    "before": original.before.model_copy(update={"size": 8 * 1024 * 1024}),
                    "after": original.after.model_copy(update={"size": 8 * 1024 * 1024}),
                }
            )
            for i in range(3)
        ),
    )
    for mutations in cases:
        record = owned.record.model_copy(
            update={"plan": owned.record.plan.model_copy(update={"mutations": mutations})}
        )
        with pytest.raises(KernelError) as caught:
            source_module._merge_versions((replace(owned, record=record),), lambda: None)
        assert caught.value.code == "git_delivery_source_limit"
