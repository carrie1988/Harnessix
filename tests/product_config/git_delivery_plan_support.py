"""正式交付计划测试的真实原 CAS 夹具；不签发批准或执行 Git 外部写入。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.models import ToolCallContent
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.delivery.contracts import MAX_WORKSPACE_DIFF_BYTES
from harnessix.delivery.git import _commit_bytes
from harnessix.delivery.git_contracts import GitCheckpoint, GitCommitSpec
from harnessix.delivery.git_inventory_contracts import (
    GitBaseHistoryBoundary,
    GitInventoryMetrics,
    GitInventoryRoots,
    GitInventoryScope,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits, verify_git_tree_closure
from harnessix.delivery.git_tree_diff import prepare_git_tree_diff
from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPolicyBinding,
    SandboxBindingV2,
    canonical_digest,
)
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.execution.versioned_contracts import ExecutionPlanV3
from harnessix.product_config.git_baseline_contracts import GitBaselineMember
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.workspace_patch_source_contracts import WorkspacePatchSourceReference
from harnessix.trusted_actions.contracts import CodingActionInvocation, build_trusted_tool_binding
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot_capture import capture_snapshot_facts
from harnessix.workspace.snapshot_v2 import _encode_snapshot
from tests.delivery.test_git_inventory_contracts import _check, _commit, _read, _reference, _tree
from tests.delivery.test_git_inventory_materials import _nodes
from tests.delivery.test_git_tree_projection import _mutation

NOW = datetime(2026, 10, 7, 8, 9, 10, tzinfo=UTC)
ZERO = "0" * 64
IMPLEMENTATION = "d" * 64


def canonical_bytes(payload):
    """独立生成规范预期字节，不调用受测 Wire 编码器。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sealed(kind, payload, field="fingerprint"):
    """先构造完整字段重算摘要，再经正式 JSON 验证器深层重建。"""
    candidate = kind.model_construct(**payload, **{field: ZERO})
    digest = canonical_digest(candidate.model_dump(mode="json", exclude={field}, warnings="error"))
    return kind.model_validate_json(
        candidate.model_copy(update={field: digest}).model_dump_json(), strict=True
    )


def rehash(payload, field="fingerprint"):
    """只重算当前对象摘要；不掩盖嵌套摘要或跨字段的错误。"""
    payload[field] = canonical_digest(
        {key: value for key, value in payload.items() if key != field}
    )
    return payload


@dataclass
class PlanCase:
    """完整材料及预期纯 Diff；身份仅用于合同测试，不代表受信会话。"""

    cas: GitMaterialCAS
    core: object
    plan: object
    bodies: tuple[GitObjectMaterial, ...]
    diff_text: str
    workspace_root: Path
    history: tuple


def _snapshot(cas, root, path, after, platform):
    """原生完整捕获并原 CAS 回读；跨平台参数仅验证逻辑元数据合同。"""
    leaf = root / path
    leaf.parent.mkdir(parents=True)
    leaf.write_bytes(after.body)
    leaf.chmod(0o755)
    (root / "keep").write_bytes(b"unchanged\n")
    facts = capture_snapshot_facts(
        root,
        cwd=".",
        resources=(WorkspaceResourceRequest(path=path, access="read"),),
        external_roots=None,
        platform=None,
        checkpoint=_check,
    )
    # 不伪造 Windows 原生验收：只从完整真实观察构造另一平台的可序列化声明。
    scope = {**facts.scope, "platform": platform}
    scope["workspace_id"] = canonical_digest(
        {key: scope[key] for key in ("platform", "root_path_digest", "root_identity")}
    )
    snapshot, blobs = _encode_snapshot(replace(facts, scope=scope), _check)
    for digest, body in blobs:
        cas.store.put_blob(digest, body)
    history = read_workspace_parent_closure(snapshot, cas.store.blob, checkpoint=_check)
    assert len(history) == len(path.split("/"))
    return snapshot, history


def _base_trees(fmt, path, old, keep):
    """以原完整 Git tree 布局构造所有父树，保留未修改的根文件。"""
    parts = path.split("/")
    child = _tree(fmt, (("100644", parts[-1].encode(), old),))
    trees = [child]
    for part in reversed(parts[1:-1]):
        child = _tree(fmt, (("40000", part.encode(), child),))
        trees.append(child)
    root = _tree(fmt, (("100644", b"keep", keep), ("40000", parts[0].encode(), child)))
    return root, (*trees, root)


def _scope(cas, fmt, action, platform, base, base_root, target, bodies, parents, limits):
    """从完整真实材料解析声明图；指标来自原全路径闭包而非受测计划端口。"""
    roots = GitInventoryRoots(_read(base), _read(base_root), _read(target), None)
    if action == "commit":
        roots = replace(roots, delivery_commit=_read(bodies[-1]))
    objects = _nodes(bodies, roots)
    catalog = tuple(node.material for node in objects)
    before = verify_git_tree_closure(
        cas,
        _reference(base_root),
        tuple(r for r in catalog if r.object_id != base_root.object_id),
        platform=platform,
        limits=limits,
        checkpoint=_check,
    )
    after = verify_git_tree_closure(
        cas,
        _reference(target),
        tuple(r for r in catalog if r.object_id != target.object_id),
        platform=platform,
        limits=limits,
        checkpoint=_check,
    )
    return GitInventoryScope(
        action,
        platform,
        roots,
        objects,
        GitBaseHistoryBoundary(
            "base_commit_parent_edges",
            _read(base),
            parents,
            tuple(sorted({parent.object_id for parent in parents})),
        ),
        limits,
        3,
        GitInventoryMetrics(
            len(objects),
            sum(m.body_bytes for m in bodies),
            sum(len(node.tree_entries) for node in objects),
            3 + (action == "commit"),
            before.expanded_entries,
            after.expanded_entries,
            before.tree_depth,
            after.tree_depth,
        ),
    )


def _commit_spec(
    delivery_id, transaction_id, base, base_root, target, mutation, message="正式交付\n"
):
    """完整原 Checkpoint、CommitSpec 与原编码器共用字节，无新的编码规则。"""
    checkpoint = sealed(
        GitCheckpoint,
        dict(
            checkpoint_id=UUID(int=90),
            worktree_id=UUID(int=91),
            worktree_plan_fingerprint="1" * 64,
            worktree_binding_digest="2" * 64,
            transaction_id=transaction_id,
            transaction_plan_fingerprint="3" * 64,
            repository_binding_digest="4" * 64,
            base_commit_oid=base.object_id,
            base_tree_oid=base_root.object_id,
            tree_oid=target.object_id,
            mutations_digest=canonical_digest([mutation.model_dump(mode="json")]),
            created_at=NOW,
        ),
        "digest",
    )
    body = _commit_bytes(
        target.object_id, base.object_id, "测试作者", "author@example.invalid", NOW, message
    )
    material = GitObjectMaterial.from_body("commit", base.object_format, body)
    spec = sealed(
        GitCommitSpec,
        dict(
            commit_id=delivery_id,
            checkpoint=checkpoint,
            branch_ref="refs/heads/product-delivery",
            parent_oid=base.object_id,
            tree_oid=target.object_id,
            author_name="测试作者",
            author_email="author@example.invalid",
            message=message,
            authored_at=NOW,
            hooks_policy="disabled",
            raw_commit_sha256=material.body_sha256,
            expected_commit_oid=material.object_id,
            implementation_digest=IMPLEMENTATION,
        ),
    )
    return spec, material


def _material_case(cas, tmp_path, fmt, platform, depth):
    """建立完整原 CAS 树、净变更及完整父历史。"""
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    path = "/".join((*["src" if n == 0 else f"d{n}" for n in range(depth)], "file.py"))
    old = GitObjectMaterial.from_body("blob", fmt, b"old\n")
    new = GitObjectMaterial.from_body("blob", fmt, "新代码🙂\n".encode())
    keep = GitObjectMaterial.from_body("blob", fmt, b"unchanged\n")
    base_root, trees = _base_trees(fmt, path, old, keep)
    width = 40 if fmt == "sha1" else 64
    parents = tuple(GitObjectRead("commit", c * width, fmt) for c in "aba")
    base = _commit(fmt, base_root, parents)
    for material in (old, new, keep, *trees, base):
        cas.persist(material)
    snapshot, history = _snapshot(cas, root, path, new, platform)
    mutation = _mutation(path, _reference(old), _reference(new), after_mode=0o755)
    limits = GitTreeClosureLimits(256, 32 * 1024 * 1024, 256, 128)
    diff = prepare_git_tree_diff(
        cas,
        _reference(base_root),
        tuple(_reference(m) for m in (old, keep, *trees[:-1])),
        (mutation,),
        (_reference(new),),
        platform=platform,
        limits=limits,
        checkpoint=_check,
        max_diff_bytes=MAX_WORKSPACE_DIFF_BYTES,
    )
    unique = {m.object_id: m for m in (old, new, keep, *trees, base, *diff.projection.new_trees)}
    for material in diff.projection.new_trees:
        cas.persist(material)
    return SimpleNamespace(
        root=root,
        path=path,
        old=old,
        new=new,
        base=base,
        base_root=base_root,
        parents=parents,
        limits=limits,
        diff=diff,
        bodies=tuple(unique.values()),
        snapshot=snapshot,
        history=history,
        mutation=mutation,
    )


def _baseline_case(graph):
    """来源和原基准摘要覆盖全部引用与观察，不建立会话认证。"""
    source = sealed(
        ProductGitDeliverySourceV2,
        dict(
            thread_id=UUID(int=4),
            workspace=graph.snapshot,
            mutations=(graph.mutation,),
            patches=(
                WorkspacePatchSourceReference(
                    turn_id=UUID(int=11),
                    call_id=UUID(int=12),
                    transaction_id=UUID(int=10),
                    route_fingerprint="3" * 64,
                    transaction_fingerprint="4" * 64,
                ),
            ),
        ),
        "digest",
    )
    return sealed(
        ProductGitDeliveryBaselineV2,
        dict(
            source=source,
            head_oid=graph.base.object_id,
            head_tree_oid=graph.base_root.object_id,
            head_ref="refs/heads/main",
            index_observation_sha256="5" * 64,
            index_observation_bytes=42,
            status_sha256="6" * 64,
            config_names_sha256="7" * 64,
            reader_binding="8" * 64,
            members=(GitBaselineMember(path=graph.path, oid=graph.old.object_id, mode="100644"),),
        ),
        "digest",
    )


def _call_case(cas, graph, action, commit_message):
    """固定模型输入及完整原 CommitSpec，不重复提交正文编码。"""
    from harnessix.product_config.git_delivery_plan_contracts import (
        ProductGitCheckpointInput,
        ProductGitCommitInput,
    )

    commit_spec = None
    bodies = graph.bodies
    if action == "commit":
        commit_spec, material = _commit_spec(
            UUID(int=1),
            UUID(int=10),
            graph.base,
            graph.base_root,
            graph.diff.projection.root,
            graph.mutation,
            commit_message,
        )
        cas.persist(material)
        bodies = (*bodies, material)
        input_model = ProductGitCommitInput(
            checkpoint_id=commit_spec.checkpoint.checkpoint_id,
            branch_ref=commit_spec.branch_ref,
            author_name=commit_spec.author_name,
            author_email=commit_spec.author_email,
            message=commit_spec.message,
            authored_at=commit_spec.authored_at,
        )
    else:
        input_model = ProductGitCheckpointInput(patches=(UUID(int=10),))
    call = ToolCallContent(
        call_id=UUID(int=6),
        provider_call_id="provider-call",
        tool="git_" + action,
        tool_version="1",
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        arguments=input_model.model_dump(mode="json"),
        requires_approval=True,
        tool_fingerprint="9" * 64,
    )
    return call, commit_spec, bodies


def _core_case(cas, graph, tmp_path, fmt, action, platform, commit_message):
    """冻结 A/D 意图及全对象范围为完整 Core，无路由摘要回边。"""
    from harnessix.product_config.git_delivery_plan_contracts import (
        GitIndexFileObservation,
        ProductGitDeliveryCore,
        ProductGitWorktreeIntent,
    )

    call, commit_spec, bodies = _call_case(cas, graph, action, commit_message)
    parent = r"C:\Harnessix\worktrees" if platform == "windows" else str(tmp_path / "worktrees")
    intents = tuple(
        ProductGitWorktreeIntent(
            role=role,
            worktree_id=UUID(int=n),
            parent_path=parent,
            parent_identity="a" * 64,
            platform=platform,
            base_commit_oid=graph.base.object_id,
        )
        for role, n in (("anchor", 7), ("delivery", 8))
    )
    scope = _scope(
        cas,
        fmt,
        action,
        platform,
        graph.base,
        graph.base_root,
        graph.diff.projection.root,
        bodies,
        graph.parents,
        graph.limits,
    )
    core = sealed(
        ProductGitDeliveryCore,
        dict(
            delivery_id=UUID(int=1),
            store_id=UUID(int=2),
            key_id=UUID(int=3),
            thread_id=UUID(int=4),
            turn_id=UUID(int=5),
            call=call,
            baseline=_baseline_case(graph),
            common_directory_path_sha256="b" * 64,
            common_directory_identity="c" * 64,
            index_file_observation=GitIndexFileObservation(
                presence="file", identity="e" * 64, sha256="f" * 64, size=42
            ),
            anchor_intent=intents[0] if action == "checkpoint" else None,
            worktree_intent=intents[1] if action == "checkpoint" else None,
            checkpoint_delivery_id=UUID(int=80) if action == "commit" else None,
            object_scope=scope,
            diff_sha256=graph.diff.content.sha256,
            diff_bytes=graph.diff.content.utf8_bytes,
            implementation_digest=IMPLEMENTATION,
            commit_spec=commit_spec,
        ),
    )
    return core, bodies


def make_case(
    cas,
    tmp_path,
    fmt="sha256",
    action="checkpoint",
    platform="posix",
    depth=2,
    commit_message="正式交付\n",
):
    """构造真实全图与正式计划封套；普通身份不是认证 Artifact 或批准。"""
    from harnessix.product_config.git_delivery_plan_contracts import ProductGitDeliveryPlan

    graph = _material_case(cas, tmp_path, fmt, platform, depth)
    core, bodies = _core_case(cas, graph, tmp_path, fmt, action, platform, commit_message)
    route = route_for(core)
    review_body = canonical_bytes({"kind": "review", "diff": graph.diff.content.text}) + b"\n"
    artifact = ArtifactRef(
        artifact_id=UUID(int=20),
        sha256=hashlib.sha256(review_body).hexdigest(),
        size_bytes=len(review_body),
        records=1,
        complete=True,
        expires_at=NOW.replace(day=8),
    )
    plan = sealed(ProductGitDeliveryPlan, dict(core=core, route=route, review_artifact=artifact))
    return PlanCase(cas, core, plan, bodies, graph.diff.content.text, graph.root, graph.history)


def route_for(core, decision=PolicyDecisionKind.REQUIRE_APPROVAL, resources=None):
    """使用原 Router/Execution 契约构造完整路由，不模拟实际批准成功。"""
    from harnessix.product_config.git_delivery_plan_contracts import product_git_delivery_resource

    call = core.call
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix",
        tool=call.tool,
        tool_version=call.tool_version,
        tool_fingerprint=call.tool_fingerprint,
        input_schema_sha256="1" * 64,
        effect_class=call.effect_class,
        risk_level=RiskLevel.HIGH,
        recovery_mode="external_reconcile",
        executor_id="product." + call.tool,
    )
    invocation_id = trusted_action_invocation_id(core.thread_id, core.turn_id, call)
    common = dict(
        source="builtin",
        source_id="harnessix",
        tool=call.tool,
        tool_version=call.tool_version,
        tool_fingerprint=call.tool_fingerprint,
        arguments=call.arguments,
        idempotency_key=str(core.delivery_id),
    )
    invocation = CodingActionInvocation(invocation_id=invocation_id, **common)
    capability = build_capability_evidence_v2(
        platform=core.baseline.source.workspace.platform,
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest="2" * 64,
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="none",
        capability_digest=capability.evidence_digest,
        profile_digest="3" * 64,
    )
    execution = sealed(
        ExecutionPlanV3,
        dict(
            plan_id=invocation_id,
            intent=ExecutionIntent(
                effect_class=call.effect_class, risk_level=RiskLevel.HIGH, **common
            ),
            workspace=core.baseline.source.workspace,
            sandbox=sandbox,
            capabilities=capability,
            policy=ExecutionPolicyBinding(
                version="1",
                decision=decision,
                policy_id="coding-risk",
                reason_code="git_write_requires_approval",
            ),
        ),
    )
    resources = (product_git_delivery_resource(core),) if resources is None else resources
    identities = [(r.kind, r.access, r.identifier_sha256, r.attributes_sha256) for r in resources]
    return sealed(
        ActionRoutePlanV2,
        dict(
            invocation=invocation,
            binding=binding,
            resources=resources,
            resources_sha256=canonical_digest(identities),
            execution=execution,
            external_action_id=core.delivery_id,
        ),
    )


def observe_state(case):
    """冻结 DB、CAS 及 Workspace 字节/写时间，不把只读访问时间算作写入。"""
    store = case.cas.store
    rows = tuple(store._db.iterdump())
    files = tuple(
        (
            str(path.relative_to(root)),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            path.stat().st_mode,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for root in (store._root, case.workspace_root)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    return rows, store._db.total_changes, files
