"""Git Review 的两种 fixture：完整 CAS 数据声明与原 SDK 认证 pending 调用。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.models import ToolCallContent, TrustedActionApprovalRequestContent
from harnessix.agent.trusted_action_runtime import TrustedActionSessionRuntime
from harnessix.delivery.git_inventory_contracts import (
    GitBaseHistoryBoundary,
    GitInventoryMetrics,
    GitInventoryRoots,
    GitInventoryScope,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits, verify_git_tree_closure
from harnessix.delivery.git_tree_diff import prepare_git_tree_diff
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryCoreV2
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitCheckpointInput,
    ProductGitWorktreeIntent,
    product_git_delivery_resource,
)
from harnessix.product_config.git_delivery_plan_materials import (
    read_product_git_delivery_core_materials_v2,
)
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_user_observation import collect_product_git_user_observation
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import build_trusted_tool_binding
from harnessix.trusted_actions.planning import external_action_identity
from harnessix.trusted_actions.router import ResolvedAction, TrustedActionDefinition
from harnessix.workspace.contracts import WorkspaceResourceRequest
from tests.delivery.test_git_inventory_contracts import _read, _reference
from tests.delivery.test_git_inventory_materials import _nodes
from tests.product_config.test_git_baseline import command
from tests.product_config.test_git_user_observation import native_identity
from tests.product_config.test_product_rollback_sdk import wait_turn
from tests.support.git_delivery_observed_core import canonical, json_facts, make_observed_case


def material_case(cas, path, **options):
    """纯声明正控只复用全材料 CAS；不提供原 Session 或批准真实性。"""
    case, _legacy = make_observed_case(cas, path, **options)
    verified = read_product_git_delivery_core_materials_v2(cas, case.core, checkpoint=lambda: None)
    return case, verified


def review_document_like(document, text, *, chunks=None, entries=None):
    """纯展示容量声明；不是完整 CAS 材料认证或生产者认证阳性。"""
    from harnessix.product_config.git_delivery_review_contracts import (
        ProductGitActionReviewDocument,
    )

    payload = json_facts(document)
    payload["summary"].update(
        diff_utf8_bytes=len(text.encode("utf-8")),
        diff_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    payload["chunks"] = (
        chunks
        if chunks is not None
        else [
            {"record_type": "text", "sequence": index, "text": text[offset : offset + 1900]}
            for index, offset in enumerate(range(0, len(text), 1900))
        ]
    )
    if entries is not None:
        payload["entries"] = entries
        payload["summary"]["file_count"] = len(entries)
    return ProductGitActionReviewDocument.model_validate_json(canonical(payload), strict=True)


def actual_base_materials(scenario, observation):
    """只读临时真实 Git 对象正文；不继承宿主 Git 配置或凭据。"""
    fmt = "sha1" if len(observation.baseline.head_oid) == 40 else "sha256"
    identifiers = command(scenario.root, "rev-list", "--objects", "HEAD").splitlines()
    materials = []
    for line in identifiers:
        oid = line.split(b" ", 1)[0].decode("ascii")
        kind = command(scenario.root, "cat-file", "-t", oid).strip().decode("ascii")
        material = GitObjectMaterial.from_body(
            kind, fmt, command(scenario.root, "cat-file", kind, oid)
        )
        assert material.object_id == oid
        materials.append(material)
    return tuple(materials)


def actual_scope(scenario, observation):
    """准备阶段把真实基线及已认证 Patch 正文写入原 CAS，再调用原完整投影。"""
    cas = GitMaterialCAS(scenario.transactions)
    baseline = observation.baseline
    bodies = actual_base_materials(scenario, observation)
    base = next(material for material in bodies if material.object_id == baseline.head_oid)
    root = next(material for material in bodies if material.object_id == baseline.head_tree_oid)
    new = tuple(
        GitObjectMaterial.from_body(
            "blob", base.object_format, scenario.transactions.blob(m.after.sha256)
        )
        for m in baseline.source.mutations
        if m.after.presence == "file"
    )
    # 相同正文可供多条路径使用；原投影目录要求材料引用唯一。
    new = tuple({material.object_id: material for material in new}.values())
    catalog = {_body.object_id: _body for _body in (*bodies, *new)}
    for material in catalog.values():
        cas.persist(material)
    limits = GitTreeClosureLimits(4096, 64 * 1024 * 1024, 4096, 128)
    diff = prepare_git_tree_diff(
        cas,
        _reference(root),
        tuple(_reference(m) for m in bodies),
        baseline.source.mutations,
        tuple(_reference(m) for m in new),
        platform=baseline.source.workspace.platform,
        limits=limits,
        checkpoint=lambda: None,
        max_diff_bytes=64 * 1024 * 1024,
    )
    for material in diff.projection.new_trees:
        cas.persist(material)
        catalog[material.object_id] = material
    roots = GitInventoryRoots(_read(base), _read(root), _read(diff.projection.root), None)
    nodes = _nodes(tuple(catalog.values()), roots)
    references = tuple(node.material for node in nodes)
    after = verify_git_tree_closure(
        cas,
        _reference(diff.projection.root),
        references,
        platform=baseline.source.workspace.platform,
        limits=limits,
        checkpoint=lambda: None,
    )
    base_node = next(node for node in nodes if node.material.object_id == base.object_id)
    parents = base_node.commit_references.parents
    scope = GitInventoryScope(
        "checkpoint",
        baseline.source.workspace.platform,
        roots,
        nodes,
        GitBaseHistoryBoundary(
            "base_commit_parent_edges",
            _read(base),
            parents,
            tuple(sorted({p.object_id for p in parents})),
        ),
        limits,
        3,
        GitInventoryMetrics(
            len(nodes),
            sum(m.body_bytes for m in catalog.values()),
            sum(len(node.tree_entries) for node in nodes),
            len(parents),
            diff.projection.base.expanded_entries,
            after.expanded_entries,
            diff.projection.base.tree_depth,
            after.tree_depth,
        ),
    )
    return scope, diff


class NeverExecuteGit:
    """只测试 pending Review；任意 Git 执行或对账调用都立即失败。"""

    async def execute(self, *args, **kwargs):
        pytest.fail("Review fixture 不得执行 Git 写动作")

    async def reconcile(self, *args, **kwargs):
        pytest.fail("Review fixture 不得触发 Git 对账")


@dataclass
class ActualCorePreparer:
    """真实 Kernel 的临时宿主准备器；认证历史仍由原 Session/Router 读取。"""

    scenario: object
    binding: object
    core_store: ProductGitDeliveryCoreStore
    core: ProductGitDeliveryCoreV2 | None = None
    diff: object = None
    error: BaseException | None = None

    async def prepare(self, invocation, arguments, context, thread, turn, call, cancel):
        try:
            return await self._prepare(invocation, arguments, context, thread, turn, call, cancel)
        except BaseException as error:
            self.error = error
            raise

    async def _prepare(self, invocation, arguments, context, thread, turn, call, cancel):
        scenario = self.scenario
        observation = await collect_product_git_user_observation(
            thread,
            tuple(arguments.patches),
            scenario.router,
            scenario.transactions,
            scenario.reader,
            session=scenario.session,
            cancel=cancel,
            budget=GitOperationBudget(60.0),
            checkpoint=cancel.checkpoint,
            snapshot_ports=scenario.router._snapshot_ports,
        )
        scope, diff = actual_scope(scenario, observation)
        parent = scenario.state / "review-worktrees"
        parent.mkdir(exist_ok=True)
        intents = tuple(
            ProductGitWorktreeIntent(
                role=role,
                worktree_id=uuid4(),
                parent_path=str(parent),
                parent_identity=native_identity(parent),
                platform=scope.platform,
                base_commit_oid=observation.baseline.head_oid,
            )
            for role in ("anchor", "delivery")
        )
        payload = json_facts(
            dict(
                spec_version="harnessix.product-git-delivery-core/v2",
                delivery_id=external_action_identity(invocation, self.binding),
                store_id=observation.store_id,
                key_id=observation.key_id,
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
                call=call,
                user_observation=observation,
                anchor_intent=intents[0],
                worktree_intent=intents[1],
                checkpoint_delivery_id=None,
                object_scope=scope,
                diff_sha256=diff.content.sha256,
                diff_bytes=diff.content.utf8_bytes,
                commit_spec=None,
                implementation_digest=observation.implementation_digest,
            )
        )
        # 只按全字段 JSON 核验事实声明；不使用 model_construct 创建认证阳性。
        payload["fingerprint"] = hashlib.sha256(canonical(payload)).hexdigest()
        core = ProductGitDeliveryCoreV2.model_validate_json(canonical(payload), strict=True)
        self.core = self.core_store.persist_v2(core, checkpoint=cancel.checkpoint)
        self.diff = read_product_git_delivery_core_materials_v2(
            GitMaterialCAS(scenario.transactions), self.core, checkpoint=cancel.checkpoint
        ).diff
        return ResolvedAction(
            resources=(product_git_delivery_resource(self.core),),
            workspace_resources=tuple(
                WorkspaceResourceRequest(location=item.location, path=item.path, access=item.access)
                for item in self.core.baseline.source.workspace.resources
            ),
            expected_workspace=self.core.baseline.source.workspace,
        )


@dataclass
class PendingActualReview:
    """来自真实 SDK waiting_approval 的事实，保留原实际 Artifact 实例。"""

    scenario: object
    preparer: ActualCorePreparer
    thread: object
    turn: object
    call: ToolCallContent
    route: object
    artifacts: object
    provider: object


async def pending_actual_review(scenario, monkeypatch, inspect, *, with_provider=False):
    """临时注册真实 Router/Gateway/Kernel 工具；不批准、不替换认证端口或历史。"""
    runtime = scenario.client.transport.server.service.runtime
    original_gateway = runtime._trusted_actions._state.gateway
    artifacts = runtime._artifacts
    core_store = ProductGitDeliveryCoreStore(scenario.transactions)
    tool = ToolDescriptor(
        name="git_checkpoint",
        version="1",
        description="准备完整 Git 审批审阅",
        input_schema=ProductGitCheckpointInput.model_json_schema(),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
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
        executor_id="test.git_review",
    )
    preparer = ActualCorePreparer(scenario, binding, core_store)

    def no_legacy_resolution(*args, **kwargs):
        pytest.fail("Git Review 应通过原 Kernel 的预规划入口")

    scenario.router.register(
        TrustedActionDefinition(
            binding,
            ProductGitCheckpointInput,
            no_legacy_resolution,
            NeverExecuteGit(),
            agent_prepare=preparer,
        )
    )
    provider = None
    if with_provider:
        from harnessix.product_config.git_delivery_review import ProductGitReviewProvider

        provider = ProductGitReviewProvider(
            scenario.router,
            core_store,
            artifacts,
            scenario.reader,
            workspace_scope=runtime.tools.workspace_scope,
            snapshot_ports=scenario.router._snapshot_ports,
        )

    def action_context(thread, turn, call):
        # 临时 Git 注册借用宿主原端口，规划仍重新真实捕获，不回填 Source 快照。
        context = original_gateway._state.context(thread, turn, call)
        return replace(context, snapshot_ports=scenario.router._snapshot_ports)

    gateway = RouterBackedAgentActionGateway(
        scenario.router,
        (*original_gateway.definitions(), tool),
        action_context,
        presentations={
            **original_gateway._state.presentations,
            tool.name: "patch_batch" if with_provider else "tool",
        },
        reviews={**original_gateway._state.reviews, **({tool.name: provider} if provider else {})},
        outputs=original_gateway._state.outputs,
        secret_scope=original_gateway._state.secret_scope,
    )
    actions = TrustedActionSessionRuntime(
        gateway, scenario.session, runtime._lock, runtime._validate_tool_contract, runtime._fault
    )
    review_errors = []
    with monkeypatch.context() as context:
        if provider is not None:
            original_review = provider.review

            async def observed_review(*args, **kwargs):
                # 只记录原方法抛出的实例供失败诊断，不替换认证或返回结果。
                try:
                    return await original_review(*args, **kwargs)
                except BaseException as error:
                    review_errors.append(error)
                    raise

            context.setattr(provider, "review", observed_review)
        context.setattr(runtime, "_trusted_actions", actions)
        context.setattr(runtime, "_definitions", {**runtime._definitions, tool.name: tool})
        scenario.bundle.steps = (
            [
                ResponseStarted(response_id="git-review-response"),
                ToolCallCompleted(
                    call_id="git-review-call",
                    tool=tool.name,
                    arguments={
                        "spec_version": "harnessix.product-git-checkpoint-input/v1",
                        "patches": [str(target) for target in scenario.targets],
                    },
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
        )
        await scenario.client.start_turn(
            scenario.thread.thread_id, "审阅 Git 交付", request_id="git-review-pending"
        )
        try:
            await wait_turn(scenario.client, scenario.thread.thread_id, "waiting_approval")
        except AssertionError:
            if preparer.error is not None:
                raise preparer.error from None
            if review_errors:
                raise review_errors[0] from None
            raise
        thread = await scenario.session.get_thread(scenario.thread.thread_id)
        turn = thread.turns[-1]
        call = next(item.content for item in turn.items if type(item.content) is ToolCallContent)
        approval = next(
            item.content
            for item in turn.items
            if type(item.content) is TrustedActionApprovalRequestContent
        )
        route = scenario.router.status(approval.plan_id)
        await inspect(
            PendingActualReview(scenario, preparer, thread, turn, call, route, artifacts, provider)
        )
