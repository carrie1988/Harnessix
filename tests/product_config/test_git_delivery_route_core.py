"""完整 Core 与原 Gateway/Router 的实际耐久接线；无 Session、MAC 或执行批准声明。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Thread,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import PolicyDecisionKind, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitCheckpointInput,
    ProductGitCommitInput,
    ProductGitDeliveryCore,
    product_git_delivery_resource,
    validate_product_git_delivery_route,
)
from harnessix.product_config.git_delivery_plan_materials import (
    verify_product_git_delivery_core_materials,
)
from harnessix.product_config.git_delivery_route_core import load_product_git_delivery_route_core
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.agent_gateway_invocation import build_agent_action_invocation
from harnessix.trusted_actions.contracts import CanonicalActionResource, build_trusted_tool_binding
from harnessix.trusted_actions.planning import _build_route, external_action_identity
from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
from harnessix.trusted_actions.router import (
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_contracts import snapshot_requests
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.product_config.git_delivery_plan_support import (
    NOW,
    canonical_bytes,
    make_case,
    observe_state,
    sealed,
)
from tests.trusted_actions.test_agent_gateway import runtime_context


def _fields(model, field="fingerprint"):
    """保留嵌套模型与 Scope 的确切类型，再交由原封签夹具验证摘要。"""
    return {name: getattr(model, name) for name in type(model).model_fields if name != field}


def _product_case(cas, path, fmt="sha256", action="checkpoint", depth=2):
    """真实 CAS 夹具的身份重绑到实际 Tool Schema/FP；不伪装成认证 Patch 来源。"""
    case = make_case(cas, path, fmt=fmt, action=action, depth=depth)
    input_model = ProductGitCheckpointInput if action == "checkpoint" else ProductGitCommitInput
    tool = ToolDescriptor(
        name=case.core.call.tool,
        version="1",
        description="准备有界Git交付完整意图",
        input_schema=input_model.model_json_schema(),
        public_output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"summary": {"type": "string", "maxLength": 128}},
        },
        effect_class=case.core.call.effect_class,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
    )
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=tool.name,
        tool_version=tool.version,
        tool_fingerprint=tool_fingerprint(tool),
        input_schema_sha256=canonical_digest(tool.input_schema),
        effect_class=tool.effect_class,
        risk_level=tool.risk_level,
        recovery_mode="external_reconcile",
        executor_id="product." + tool.name,
    )
    call = case.core.call.model_copy(update={"tool_fingerprint": tool_fingerprint(tool)}, deep=True)
    turn = Turn(
        turn_id=case.core.turn_id,
        request_id="core-route-request",
        request_fingerprint="1" * 64,
        status=TurnStatus.EXECUTING_TOOLS,
        budget=Budget(),
        created_at=NOW,
    )
    thread = Thread(
        thread_id=case.core.thread_id,
        workspace=str(case.workspace_root),
        active_turn_id=turn.turn_id,
        turns=(turn,),
        created_at=NOW,
        updated_at=NOW,
    )
    invocation = build_agent_action_invocation(
        thread, turn, call, binding, requires_idempotency=True
    )
    delivery_id = external_action_identity(invocation, binding)
    payload = _fields(case.core)
    payload.update(call=call, delivery_id=delivery_id)
    if case.core.commit_spec is not None:
        payload["commit_spec"] = sealed(
            type(case.core.commit_spec),
            {
                **_fields(case.core.commit_spec),
                "commit_id": delivery_id,
            },
        )
    core = sealed(ProductGitDeliveryCore, payload)
    return SimpleNamespace(
        case=case,
        core=core,
        tool=tool,
        binding=binding,
        input_model=input_model,
        thread=thread,
        turn=turn,
        call=call,
        invocation=invocation,
    )


def _original_route(product):
    """单元声明仍用原规划算法构造；实际持久接线在独立 Gateway 测试执行。"""
    context = runtime_context(product.case.workspace_root)
    resources = (product_git_delivery_resource(product.core),)
    policy = DefaultCodingRiskPolicy().evaluate(product.binding, resources, context.sandbox, ())
    return _build_route(
        product.invocation,
        product.binding,
        resources,
        context,
        policy,
        product.core.baseline.source.workspace,
    )


def _rebuild_route(route, **updates):
    return sealed(ActionRoutePlanV2, {**_fields(route), **updates})


def _resource_route(route, resources, **updates):
    return _rebuild_route(
        route,
        resources=resources,
        resources_sha256=canonical_digest(
            [(r.kind, r.access, r.identifier_sha256, r.attributes_sha256) for r in resources]
        ),
        **updates,
    )


def _reject(owner, route, code="git_delivery_plan_invalid"):
    with pytest.raises(KernelError) as caught:
        load_product_git_delivery_route_core(owner, route, checkpoint=lambda: None)
    assert caught.value.code == code
    assert caught.value.__cause__ is None
    assert "新代码" not in str(caught.value)
    assert "private-sensitive" not in str(caught.value)
    return caught.value


@pytest.fixture
def loaded_case(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas") as store:
        product = _product_case(GitMaterialCAS(store), tmp_path)
        owner = ProductGitDeliveryCoreStore(store)
        owner.persist(product.core, checkpoint=lambda: None)
        yield product, owner, _original_route(product)


def test_complete_read_returns_detached_core_and_does_not_write(loaded_case):
    product, owner, route = loaded_case
    before = observe_state(product.case)
    restored = load_product_git_delivery_route_core(owner, route, checkpoint=lambda: None)
    assert restored == product.core and restored is not product.core
    assert restored.call.arguments is not route.invocation.arguments
    assert restored.baseline.source.workspace is not route.execution.workspace
    assert restored.object_scope is not product.core.object_scope
    assert owner.store.blob(restored.fingerprint) == canonical_bytes(
        product.core.model_dump(mode="json", exclude={"fingerprint"})
    )
    assert observe_state(product.case) == before
    restored.call.arguments["patches"].append(str(UUID(int=111)))
    assert (
        load_product_git_delivery_route_core(owner, route, checkpoint=lambda: None) == product.core
    )
    assert not hasattr(restored, "approved") and not hasattr(restored, "mac_verified")


def test_valid_original_route_one_is_not_upgraded_or_accepted(loaded_case):
    product, owner, _route = loaded_case
    context = runtime_context(product.case.workspace_root)
    resources = (product_git_delivery_resource(product.core),)
    workspace = capture_workspace_snapshot(
        context.workspace_root,
        resources=snapshot_requests(product.core.baseline.source.workspace),
    )
    policy = DefaultCodingRiskPolicy().evaluate(product.binding, resources, context.sandbox, ())
    legacy = _build_route(
        product.invocation, product.binding, resources, context, policy, workspace
    )
    assert legacy.spec_version == "harnessix.action-route-plan/v1"
    _reject(owner, legacy)


@pytest.mark.parametrize(
    "field", ["store_id", "key_id", "delivery_id", "thread_id", "turn_id", "call_id"]
)
def test_each_core_identity_is_bound_by_full_resource_not_only_attributes(loaded_case, field):
    product, owner, route = loaded_case
    core = product.core
    payload = _fields(core)
    if field == "call_id":
        payload["call"] = core.call.model_copy(update={"call_id": UUID(int=110)})
    elif field == "thread_id":
        source = sealed(
            type(core.baseline.source),
            {
                **_fields(core.baseline.source, "digest"),
                "thread_id": UUID(int=110),
            },
            "digest",
        )
        payload.update(
            thread_id=UUID(int=110),
            baseline=sealed(
                type(core.baseline),
                {
                    **_fields(core.baseline, "digest"),
                    "source": source,
                },
                "digest",
            ),
        )
    else:
        payload[field] = UUID(int=110)
    other = sealed(ProductGitDeliveryCore, payload)
    owner.persist(other, checkpoint=lambda: None)
    resource = route.resources[0].model_copy(update={"attributes_sha256": other.fingerprint})
    altered = _resource_route(route, (resource,))
    _reject(owner, altered)


@pytest.mark.parametrize(
    "kind,access", [("external", "read"), ("workspace", "write"), ("git_ref", "update")]
)
def test_only_single_external_write_resource_is_admitted_before_cas(
    loaded_case, monkeypatch, kind, access
):
    _product, owner, route = loaded_case
    resource = route.resources[0].model_copy(update={"kind": kind, "access": access})
    altered = _resource_route(route, (resource,))
    monkeypatch.setattr(
        ProductGitDeliveryCoreStore, "load", lambda *_a, **_k: pytest.fail("不可读取CAS")
    )
    _reject(owner, altered)


@pytest.mark.parametrize("extra", [False, True])
def test_no_empty_or_extra_resource_even_with_original_core_digest(loaded_case, monkeypatch, extra):
    _product, owner, route = loaded_case
    added = CanonicalActionResource(
        kind="workspace", access="read", identifier_sha256="f" * 64, attributes_sha256="e" * 64
    )
    resources = (
        tuple(
            sorted(
                (*route.resources, added),
                key=lambda item: (
                    item.kind,
                    item.access,
                    item.identifier_sha256,
                    item.attributes_sha256,
                ),
            )
        )
        if extra
        else ()
    )
    altered = _resource_route(route, resources)
    monkeypatch.setattr(
        ProductGitDeliveryCoreStore, "load", lambda *_a, **_k: pytest.fail("不可读取CAS")
    )
    _reject(owner, altered)


@pytest.mark.parametrize("field", ["identifier_sha256", "attributes_sha256"])
def test_each_resource_digest_is_independently_checked(loaded_case, field):
    _product, owner, route = loaded_case
    resource = route.resources[0].model_copy(update={field: "a" * 64})
    altered = _resource_route(route, (resource,))
    _reject(
        owner,
        altered,
        "git_delivery_core_read_failed"
        if field == "attributes_sha256"
        else "git_delivery_plan_invalid",
    )


def test_matching_fake_delivery_uuid_cannot_replace_original_router_derivation(loaded_case):
    product, owner, route = loaded_case
    fake = sealed(
        ProductGitDeliveryCore,
        {**_fields(product.core), "delivery_id": UUID(int=110)},
    )
    owner.persist(fake, checkpoint=lambda: None)
    resource = product_git_delivery_resource(fake)
    altered = _resource_route(route, (resource,), external_action_id=fake.delivery_id)
    # 原封套声明条件仍全部满足；原 Router 的实际派生身份是另一个必要入口事实。
    validate_product_git_delivery_route(fake, altered)
    _reject(owner, altered)


@pytest.mark.parametrize("decision", [PolicyDecisionKind.ALLOW, PolicyDecisionKind.DENY])
def test_rehashed_route_with_wrong_policy_is_rejected(loaded_case, decision):
    _product, owner, route = loaded_case
    execution = sealed(
        type(route.execution),
        {
            **_fields(route.execution),
            "policy": route.execution.policy.model_copy(update={"decision": decision}),
        },
    )
    _reject(owner, _rebuild_route(route, execution=execution))


def test_rehashed_route_arguments_cannot_replace_complete_core_call(loaded_case):
    _product, owner, route = loaded_case
    arguments = {**route.invocation.arguments, "patches": [str(UUID(int=111))]}
    execution = sealed(
        type(route.execution),
        {
            **_fields(route.execution),
            "intent": route.execution.intent.model_copy(update={"arguments": arguments}),
        },
    )
    altered = _rebuild_route(
        route,
        invocation=route.invocation.model_copy(update={"arguments": arguments}),
        execution=execution,
    )
    _reject(owner, altered)


@pytest.mark.parametrize("identity", ["invocation", "external"])
def test_even_resealed_route_requires_original_stable_identities(loaded_case, identity):
    _product, owner, route = loaded_case
    if identity == "external":
        altered = _rebuild_route(route, external_action_id=UUID(int=111))
    else:
        invocation = route.invocation.model_copy(update={"invocation_id": UUID(int=111)})
        execution = sealed(
            type(route.execution), {**_fields(route.execution), "plan_id": UUID(int=111)}
        )
        altered = _rebuild_route(
            route,
            invocation=invocation,
            execution=execution,
            external_action_id=external_action_identity(invocation, route.binding),
        )
    _reject(owner, altered)


@pytest.mark.parametrize("owner_shape", ["duck", "subclass"])
def test_loader_requires_exact_core_store_not_a_substituted_load_callback(loaded_case, owner_shape):
    _product, _owner, route = loaded_case
    if owner_shape == "subclass":

        class StoreSubclass(ProductGitDeliveryCoreStore):
            def load(self, *_a, **_k):
                pytest.fail("不可调用替代Core读取")

        owner = object.__new__(StoreSubclass)
    else:
        owner = SimpleNamespace(load=lambda *_a, **_k: pytest.fail("不可调用替代Core读取"))
    _reject(owner, route)


def test_rehashed_workspace_change_is_rejected(loaded_case):
    _product, owner, route = loaded_case
    payload = route.execution.workspace.model_dump(exclude={"revision"})
    payload["root_identity"] = "a" * 64
    payload["workspace_id"] = canonical_digest(
        {
            key: payload[key]
            for key in (
                "platform",
                "root_path_digest",
                "root_identity",
            )
        }
    )
    payload["revision"] = canonical_digest(
        {key: value for key, value in payload.items() if key != "spec_version"}
    )
    workspace = type(route.execution.workspace).model_validate(payload)
    execution = sealed(
        type(route.execution),
        {**_fields(route.execution), "workspace": workspace},
    )
    _reject(owner, _rebuild_route(route, execution=execution))


@pytest.mark.parametrize(
    "field", ["fingerprint", "resources_sha256", "binding", "invocation", "execution"]
)
def test_nested_route_fingerprints_and_crossfields_are_revalidated_before_io(
    loaded_case, monkeypatch, field
):
    _product, owner, route = loaded_case
    if field in {"fingerprint", "resources_sha256"}:
        altered = route.model_copy(update={field: "a" * 64})
    elif field == "binding":
        altered = route.model_copy(
            update={field: route.binding.model_copy(update={"binding_digest": "a" * 64})}
        )
    elif field == "invocation":
        altered = route.model_copy(
            update={field: route.invocation.model_copy(update={"tool_version": "2"})}
        )
    else:
        altered = route.model_copy(
            update={field: route.execution.model_copy(update={"fingerprint": "a" * 64})}
        )
    monkeypatch.setattr(
        ProductGitDeliveryCoreStore, "load", lambda *_a, **_k: pytest.fail("不可读取CAS")
    )
    _reject(owner, altered)


@pytest.mark.parametrize(
    "shape",
    ["dict", "subclass", "extra", "missing", "list", "nested_extra", "uuid_string", "enum_string"],
)
def test_exact_route_two_and_all_nested_actual_types_are_required(loaded_case, monkeypatch, shape):
    _product, owner, route = loaded_case
    altered = route.model_copy(deep=True)
    if shape == "dict":
        altered = route.model_dump(mode="json")
    elif shape == "subclass":

        class RouteSubclass(ActionRoutePlanV2):
            pass

        altered = RouteSubclass.model_validate(route.model_dump())
    elif shape == "extra":
        vars(altered)["private_sensitive"] = "private-sensitive"
    elif shape == "missing":
        del vars(altered)["external_action_id"]
    elif shape == "list":
        object.__setattr__(altered, "resources", list(altered.resources))
    elif shape == "nested_extra":
        vars(altered.resources[0])["private_sensitive"] = "private-sensitive"
    elif shape == "uuid_string":
        object.__setattr__(
            altered.invocation, "invocation_id", str(altered.invocation.invocation_id)
        )
    else:
        object.__setattr__(altered.execution.policy, "decision", "require_approval")
    monkeypatch.setattr(
        ProductGitDeliveryCoreStore, "load", lambda *_a, **_k: pytest.fail("不可读取CAS")
    )
    _reject(owner, altered)


@pytest.mark.parametrize("bad", ["missing", "noncanonical", "wrong_raw_sha", "invalid_core"])
def test_original_cas_missing_or_bad_full_body_is_not_replaced(loaded_case, bad):
    product, owner, route = loaded_case
    path = owner.store._blobs / product.core.fingerprint
    if bad == "missing":
        path.unlink()
    elif bad == "wrong_raw_sha":
        path.write_bytes(b"private-sensitive")
    else:
        payload = product.core.model_dump(mode="json", exclude={"fingerprint"})
        if bad == "invalid_core":
            payload["call"]["requires_approval"] = False
        body = (
            json.dumps(payload, ensure_ascii=False, indent=2).encode()
            if bad == "noncanonical"
            else canonical_bytes(payload)
        )
        fingerprint = hashlib.sha256(body).hexdigest()
        owner.store.put_blob(fingerprint, body)
        resource = route.resources[0].model_copy(update={"attributes_sha256": fingerprint})
        route = _resource_route(route, (resource,))
    _reject(owner, route, "git_delivery_core_read_failed")


@pytest.mark.parametrize(
    "error",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        TimeoutError("deadline"),
        ValueError("callback"),
        KernelError("test_cancel", "取消"),
    ],
)
@pytest.mark.parametrize("stage", ["snapshot", "cas", "last"])
def test_original_checkpoint_exception_identity_is_preserved(
    loaded_case, monkeypatch, error, stage
):
    _product, owner, route = loaded_case
    total = 0

    def count():
        nonlocal total
        total += 1

    load_product_git_delivery_route_core(owner, route, checkpoint=count)
    stop = 1 if stage == "snapshot" else total
    calls = 0
    in_cas = False
    original_load = ProductGitDeliveryCoreStore.load

    def actual_load(self, *args, **kwargs):
        nonlocal in_cas
        in_cas = True
        try:
            return original_load(self, *args, **kwargs)
        finally:
            in_cas = False

    monkeypatch.setattr(ProductGitDeliveryCoreStore, "load", actual_load)

    def check():
        nonlocal calls
        calls += 1
        if (stage == "cas" and in_cas) or (stage != "cas" and calls == stop):
            raise error

    with pytest.raises(type(error)) as caught:
        load_product_git_delivery_route_core(owner, route, checkpoint=check)
    assert caught.value is error


class _NoEffects:
    """执行与对账均必须保持未调用；不制造任何外部效果。"""

    calls = 0

    async def execute(self, *_args):
        self.calls += 1
        pytest.fail("等待审批不可执行")

    async def reconcile(self, *_args):
        self.calls += 1
        pytest.fail("等待审批不可对账")


class _PrepareCore:
    """首次注册准备器实际核验完整原材料并先耐久 Core，重开后禁止再调用。"""

    def __init__(self, product, owner, trace, *, bomb=False):
        self.product, self.owner, self.trace, self.bomb = product, owner, trace, bomb
        self.calls = 0

    async def prepare(self, invocation, arguments, context, thread, turn, call, cancel):
        self.calls += 1
        if self.bomb:
            pytest.fail("已有Route不可重新观察和准备Core")
        assert context.checkpoint is not None
        context.checkpoint()
        cancel.checkpoint()
        assert invocation == self.product.invocation and call == self.product.call
        assert (
            thread.thread_id == self.product.core.thread_id
            and turn.turn_id == self.product.core.turn_id
        )
        assert arguments == self.product.input_model.model_validate_json(json.dumps(call.arguments))
        verified = verify_product_git_delivery_core_materials(
            GitMaterialCAS(self.owner.store),
            self.product.core,
            checkpoint=context.checkpoint,
        )
        self.trace.append("materials")
        saved = self.owner.persist(verified, checkpoint=context.checkpoint)
        self.trace.append("core")
        return ResolvedAction(
            resources=(product_git_delivery_resource(saved),),
            workspace_resources=snapshot_requests(saved.baseline.source.workspace),
            expected_workspace=saved.baseline.source.workspace,
        )


@contextmanager
def _gateway(product, owner, prepare, trace):
    """真正原 Execution/Audit Stores 与 Gateway；只读原 Route，不发布 Artifact。"""
    root, state = product.case.workspace_root, owner.store._root.parent
    ports = WorkspaceSnapshotPorts(owner.store.put_blob, owner.store.blob)
    context = replace(runtime_context(root), snapshot_ports=ports)
    effects = _NoEffects()
    with (
        SQLiteExecutionPlanStore(state / "execution.db", read_blob=ports.read_blob) as plans,
        SQLiteActionAuditStore(state / "audit.db", read_blob=ports.read_blob) as audit,
    ):
        router = TrustedActionRouter(
            plans=plans, audit=audit, workspace_root=lambda _: root, snapshot_ports=ports
        )

        def no_resolve(*_args):
            pytest.fail("注册准备器不能退回同步Resolve")

        router.register(
            TrustedActionDefinition(
                product.binding,
                product.input_model,
                no_resolve,
                effects,
                agent_prepare=prepare,
            )
        )
        gateway = RouterBackedAgentActionGateway(router, (product.tool,), lambda *_: context)

        def route_written(sql):
            if sql.startswith("INSERT INTO action_route_plans "):
                assert owner.load(product.core.fingerprint, checkpoint=lambda: None) == product.core
                trace.append("route")

        audit._db.set_trace_callback(route_written)
        plans._db.set_trace_callback(
            lambda sql: (
                trace.append("execution")
                if sql.startswith("INSERT INTO execution_plans ")
                else None
            )
        )
        try:
            yield SimpleNamespace(
                gateway=gateway, router=router, plans=plans, audit=audit, effects=effects
            )
        finally:
            gateway.close()
            assert effects.calls == 0
            assert plans._db.execute("SELECT COUNT(*) FROM execution_approvals").fetchone() == (0,)
            assert owner.store._db.execute(
                "SELECT COUNT(*) FROM workspace_transactions"
            ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("depth", [2, 63])
async def test_actual_gateway_core_before_route_reopen_query_first_full_read(
    tmp_path, fmt, action, depth
):
    """实际审批请求非批准；完整父历史及对象引用关闭重开后保持，没有 Git 写。"""
    root = tmp_path / "cas"
    trace = []
    with SQLiteWorkspaceTransactionStore(root) as store:
        product = _product_case(GitMaterialCAS(store), tmp_path, fmt, action, depth)
        owner = ProductGitDeliveryCoreStore(store)
        prepare = _PrepareCore(product, owner, trace)
        assert product.call.tool_fingerprint != "9" * 64
        assert product.core.delivery_id == uuid5(
            UUID("03e94e61-ab1c-4af5-b3da-46bf017b07b2"),
            f"{product.invocation.invocation_id}:{product.binding.binding_digest}",
        )
        assert product.binding.input_schema_sha256 == canonical_digest(product.tool.input_schema)
        assert not (store._blobs / product.core.fingerprint).exists()
        with _gateway(product, owner, prepare, trace) as first:
            approval = await first.gateway.prepare(
                product.thread, product.turn, product.call, CancelToken()
            )
            assert isinstance(approval, TrustedActionApprovalRequestContent)
            assert (
                approval.presentation == "tool"
                and approval.diff_artifact is None
                and approval.decision is None
            )
            assert prepare.calls == 1 and trace == ["materials", "core", "route", "execution"]
            route = first.router.status(approval.plan_id)
            assert route.state == "pending_approval" and type(route.plan) is ActionRoutePlanV2
            assert first.plans.load_plan(approval.plan_id) == route.plan.execution
            assert first.router.approval(approval.plan_id) is None
            before = observe_state(product.case)
            restored = load_product_git_delivery_route_core(
                owner, route.plan, checkpoint=lambda: None
            )
            assert restored == product.core and observe_state(product.case) == before
            body = store.blob(restored.fingerprint)
            manifest = store.blob(restored.baseline.source.workspace.parent_closure.sha256)
            chunks = tuple(
                (row["sha256"], store.blob(row["sha256"])) for row in json.loads(manifest)["chunks"]
            )
            assert (
                len(
                    read_workspace_parent_closure(
                        restored.baseline.source.workspace, store.blob, checkpoint=lambda: None
                    )
                )
                == depth + 1
            )
            assert not {"route", "review_artifact", "fingerprint"} & json.loads(body).keys()
        assert (
            not (tmp_path / "worktrees").exists()
            and not (product.case.workspace_root / ".git").exists()
        )
    trace.clear()
    with SQLiteWorkspaceTransactionStore(root) as reopened:
        owner = ProductGitDeliveryCoreStore(reopened)
        bomb = _PrepareCore(product, owner, trace, bomb=True)
        with _gateway(product, owner, bomb, trace) as second:
            repeated = await second.gateway.prepare(
                product.thread, product.turn, product.call, CancelToken()
            )
            assert repeated == approval and bomb.calls == 0 and trace == []
            durable_route = second.router.status(repeated.plan_id)
            assert durable_route == route
            restored = load_product_git_delivery_route_core(
                owner, durable_route.plan, checkpoint=lambda: None
            )
            assert restored == product.core and reopened.blob(restored.fingerprint) == body
            assert (
                reopened.blob(restored.baseline.source.workspace.parent_closure.sha256) == manifest
            )
            assert tuple((digest, reopened.blob(digest)) for digest, _ in chunks) == chunks
            assert (
                read_workspace_parent_closure(
                    restored.baseline.source.workspace, reopened.blob, checkpoint=lambda: None
                )
                == product.case.history
            )
            assert second.plans.load_plan(repeated.plan_id) == route.plan.execution
            assert second.router.approval(repeated.plan_id) is None
