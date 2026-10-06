"""Git 产品完整意图的跨字段校验；复用原数据算法，不授予归属或执行权限。"""

from __future__ import annotations

import json
from collections.abc import Callable

from harnessix.delivery.git_inventory_contracts import snapshot_git_inventory_scope
from harnessix.domain.models import EffectClass
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitCheckpointInput,
    ProductGitCommitInput,
    ProductGitDeliveryCore,
    product_git_delivery_core_fingerprint,
)


def validate_product_git_delivery_core(
    core: ProductGitDeliveryCore, checkpoint: Callable[[], None]
) -> None:
    """完整来源、调用、对象根及原指纹共同验证，不补造未提供的事实。"""
    source, scope = core.baseline.source, core.object_scope
    snapshot_git_inventory_scope(scope, checkpoint=checkpoint)
    if (
        core.thread_id != source.thread_id
        or scope.platform != source.workspace.platform
        or (scope.roots.base_commit.object_id, scope.roots.base_tree.object_id)
        != (core.baseline.head_oid, core.baseline.head_tree_oid)
        or not core.call.requires_approval
        or core.call.effect_class is EffectClass.READ_ONLY
        or core.call.tool_fingerprint is None
        or core.call.tool != "git_" + scope.action_kind
    ):
        raise ValueError("Git交付Core的来源、对象或调用不一致")
    if scope.action_kind == "checkpoint":
        validate_checkpoint_core(core)
    else:
        validate_commit_core(core)
    if core.fingerprint != product_git_delivery_core_fingerprint(core):
        raise ValueError("Git交付Core完整指纹不一致")


def validate_checkpoint_core(core: ProductGitDeliveryCore) -> None:
    """新 A 与 D 的全部意图必须由同一 Checkpoint 调用覆盖。"""
    anchor, delivery = core.anchor_intent, core.worktree_intent
    if (
        anchor is None
        or delivery is None
        or core.commit_spec is not None
        or core.checkpoint_delivery_id is not None
        or (anchor.role, delivery.role) != ("anchor", "delivery")
        or anchor.worktree_id == delivery.worktree_id
        or any(
            (item.platform, item.base_commit_oid)
            != (core.object_scope.platform, core.baseline.head_oid)
            for item in (anchor, delivery)
        )
    ):
        raise ValueError("Git Checkpoint必须绑定新A/D及完整原基准")
    value = ProductGitCheckpointInput.model_validate_json(json.dumps(core.call.arguments))
    if set(value.patches) != {patch.transaction_id for patch in core.baseline.source.patches}:
        raise ValueError("Git Checkpoint选择与完整原Patch集合不一致")


def validate_commit_core(core: ProductGitDeliveryCore) -> None:
    """Commit 不创建新 A/D，不继承 Checkpoint 的批准。"""
    spec, scope = core.commit_spec, core.object_scope
    if (
        spec is None
        or core.checkpoint_delivery_id is None
        or core.anchor_intent is not None
        or core.worktree_intent is not None
        or scope.roots.delivery_commit is None
        or (spec.parent_oid, spec.tree_oid, spec.expected_commit_oid)
        != (
            core.baseline.head_oid,
            scope.roots.target_tree.object_id,
            scope.roots.delivery_commit.object_id,
        )
        or spec.checkpoint.base_tree_oid != core.baseline.head_tree_oid
        or spec.checkpoint.mutations_digest
        != canonical_digest(
            [
                mutation.model_dump(mode="json", warnings="error")
                for mutation in core.baseline.source.mutations
            ]
        )
        or spec.implementation_digest != core.implementation_digest
    ):
        raise ValueError("Git Commit必须绑定完整原Checkpoint与新提交对象")
    value = ProductGitCommitInput.model_validate_json(json.dumps(core.call.arguments))
    if value.authored_at.isoformat() != spec.authored_at.isoformat() or value.model_dump(
        exclude={"spec_version"}
    ) != {
        "checkpoint_id": spec.checkpoint.checkpoint_id,
        **{
            name: getattr(spec, name)
            for name in ("branch_ref", "author_name", "author_email", "message", "authored_at")
        },
    }:
        raise ValueError("Git Commit调用与正式提交字段不一致")
