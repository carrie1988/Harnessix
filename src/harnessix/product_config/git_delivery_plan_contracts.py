"""完整 Git 交付意图与审批封套；声明摘要不授予归属、原生绑定或写权限。"""

from __future__ import annotations

import ntpath
import posixpath
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    ValidationInfo,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.models import ToolCallContent
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    MAX_WORKSPACE_DIFF_BYTES,
    DeliveryContract,
)
from harnessix.delivery.git_contracts import GitCommitSpec, validate_git_branch_ref
from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.domain.models import PolicyDecisionKind
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_parent_contracts import ProductGitDeliveryBaselineV2
from harnessix.tools.contracts import Revision
from harnessix.trusted_actions.contracts import CanonicalActionResource
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.contracts import PlatformKind


class ProductGitCheckpointInput(DeliveryContract):
    """模型只能显式选择原 Patch；最终顺序由认证 Session 决定。"""

    spec_version: Literal["harnessix.product-git-checkpoint-input/v1"] = (
        "harnessix.product-git-checkpoint-input/v1"
    )
    patches: tuple[UUID, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_patches(self) -> Self:
        if len(set(self.patches)) != len(self.patches):
            raise ValueError("Git Checkpoint必须显式选择唯一Patch")
        return self


class ProductGitCommitInput(DeliveryContract):
    """有限新提交意图；Checkpoint UUID 本身不证明 Session 归属。"""

    spec_version: Literal["harnessix.product-git-commit-input/v1"] = (
        "harnessix.product-git-commit-input/v1"
    )
    checkpoint_id: UUID
    branch_ref: str = Field(min_length=12, max_length=1024)
    author_name: str = Field(min_length=1, max_length=256)
    author_email: str = Field(min_length=3, max_length=320)
    message: str = Field(min_length=1, max_length=65536, repr=False)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def finite_commit(self) -> Self:
        validate_git_branch_ref(self.branch_ref)
        if (
            any(c in self.author_name for c in "\0\n\r<>")
            or any(c in self.author_email for c in "\0\n\r<> \t")
            or "@" not in self.author_email
            or "\0" in self.message
            or not self.message.endswith("\n")
            or (offset := self.authored_at.utcoffset()) is None
            or offset.total_seconds() % 60
        ):
            raise ValueError("Git Commit输入不满足正式提交编码契约")
        return self


class GitIndexFileObservation(DeliveryContract):
    """U 的物理 Index 观察；与逻辑索引摘要分离，不用于修改或恢复 Index。"""

    presence: Literal["absent", "file"]
    identity: Revision | None
    sha256: Revision | None
    size: int = Field(ge=0, le=MAX_TRANSACTION_FILE_BYTES)

    @model_validator(mode="after")
    def paired_presence(self) -> Self:
        valid = (
            self.identity is None and self.sha256 is None and self.size == 0
            if self.presence == "absent"
            else self.identity is not None and self.sha256 is not None
        )
        if not valid:
            raise ValueError("Git物理Index存在性与完整观察不一致")
        return self


class ProductGitWorktreeIntent(DeliveryContract):
    """受信规划冻结的新目录意图；实际父身份及目标不存在仍须原生复核。"""

    role: Literal["anchor", "delivery"]
    worktree_id: UUID
    parent_path: str = Field(min_length=1, max_length=4059, repr=False)
    parent_identity: Revision
    platform: PlatformKind
    base_commit_oid: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    expected_missing: Literal[True] = True

    @model_validator(mode="after")
    def fixed_target(self) -> Self:
        path = ntpath if self.platform == "windows" else posixpath
        if (
            not path.isabs(self.parent_path)
            or path.normpath(self.parent_path) != self.parent_path
            or any(ord(c) < 32 or ord(c) == 127 for c in self.parent_path)
            or (self.platform == "windows" and not ntpath.splitdrive(self.parent_path)[0])
        ):
            raise ValueError("Git私有目标父目录必须为规范绝对路径")
        return self

    @property
    def path(self) -> str:
        """目标只能由受信父目录与新 UUID 派生，不接受第二个路径字段。"""
        if self.platform == "windows":
            return ntpath.join(self.parent_path, str(self.worktree_id))
        return posixpath.join(self.parent_path, str(self.worktree_id))


class ProductGitDeliveryCore(DeliveryContract):
    """完整执行事实先于 Route 冻结，避免 Core 与 Route 互相包含摘要。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        ser_json_bytes="hex",
        val_json_bytes="hex",
    )
    spec_version: Literal["harnessix.product-git-delivery-core/v1"] = (
        "harnessix.product-git-delivery-core/v1"
    )
    delivery_id: UUID
    store_id: UUID
    key_id: UUID
    thread_id: UUID
    turn_id: UUID
    call: ToolCallContent
    baseline: ProductGitDeliveryBaselineV2
    common_directory_path_sha256: Revision
    common_directory_identity: Revision
    index_file_observation: GitIndexFileObservation
    anchor_intent: ProductGitWorktreeIntent | None
    worktree_intent: ProductGitWorktreeIntent | None
    checkpoint_delivery_id: UUID | None
    object_scope: GitInventoryScope = Field(repr=False)
    diff_sha256: Revision
    diff_bytes: int = Field(ge=1, le=MAX_WORKSPACE_DIFF_BYTES)
    commit_spec: GitCommitSpec | None
    implementation_digest: Revision
    fingerprint: Revision

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """新 Core 的 Scope 全字段/hex Schema；不改变旧目录的 name_hex Wire。"""
        result = handler(schema)
        root = handler.resolve_ref_schema(result)
        _scope_schema(root["properties"]["object_scope"], handler, set())
        return result

    @model_validator(mode="after")
    def complete_core(self, info: ValidationInfo) -> Self:
        """在原验证检查点下执行唯一完整交叉字段算法，不把模型当执行准入。"""
        from harnessix.product_config.git_delivery_plan_validation import (
            validate_product_git_delivery_core,
        )

        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        validate_product_git_delivery_core(self, checkpoint)
        return self


def product_git_delivery_core_fingerprint(core: ProductGitDeliveryCore) -> str:
    """覆盖完整 Scope、父引用、来源、目标与提交，不包括后生成的 Route。"""
    return canonical_digest(core.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))


def _scope_schema(schema: JsonSchemaValue, handler: GetJsonSchemaHandler, seen: set[int]) -> None:
    """只遍历新 Scope 的有限无环类型图，声明字节策略及完整字段拒绝语义。"""
    schema = handler.resolve_ref_schema(schema)
    if id(schema) in seen:
        return
    seen.add(id(schema))
    properties = schema.get("properties")
    if properties is not None:
        schema["additionalProperties"] = False
        if "name" in properties and properties["name"].get("format") == "binary":
            properties["name"].update(format="hex", pattern=r"^(?:[0-9a-f]{2})*$")
        for child in properties.values():
            _scope_schema(child, handler, seen)
    if isinstance(schema.get("items"), dict):
        _scope_schema(schema["items"], handler, seen)
    for key in ("anyOf", "allOf", "oneOf"):
        for child in schema.get(key, ()):
            _scope_schema(child, handler, seen)


def product_git_delivery_resource(core: ProductGitDeliveryCore) -> CanonicalActionResource:
    """绑定原 Router 资源；后续封套不得加入 Core 以外的执行意图。"""
    return CanonicalActionResource(
        kind="external",
        access="write",
        identifier_sha256=canonical_digest(
            {
                "store_id": str(core.store_id),
                "key_id": str(core.key_id),
                "delivery_id": str(core.delivery_id),
                "thread_id": str(core.thread_id),
                "turn_id": str(core.turn_id),
                "call_id": str(core.call.call_id),
            }
        ),
        attributes_sha256=core.fingerprint,
    )


class ProductGitDeliveryPlan(DeliveryContract):
    """Core → 原 Route → 完整 Review 封套；封套本身不是执行批准。"""

    spec_version: Literal["harnessix.product-git-delivery-plan/v1"] = (
        "harnessix.product-git-delivery-plan/v1"
    )
    core: ProductGitDeliveryCore
    route: ActionRoutePlanV2
    review_artifact: ArtifactRef
    fingerprint: Revision

    @model_validator(mode="after")
    def bound_envelope(self) -> Self:
        validate_product_git_delivery_route(self.core, self.route)
        if (
            not self.review_artifact.complete
            or self.review_artifact.records < 1
            or self.review_artifact.size_bytes < 1
            or self.fingerprint != product_git_delivery_plan_fingerprint(self)
        ):
            raise ValueError("Git计划必须由原Route绑定完整Core及Review")
        return self


def validate_product_git_delivery_route(
    core: ProductGitDeliveryCore, route: ActionRoutePlanV2
) -> None:
    """封套与原 CAS 读回共用的交叉字段算法；不替代严格快照或认证读取。"""
    call, invocation = core.call, route.invocation
    if (
        invocation.invocation_id != trusted_action_invocation_id(core.thread_id, core.turn_id, call)
        or (invocation.tool, invocation.tool_version, invocation.tool_fingerprint)
        != (call.tool, call.tool_version, call.tool_fingerprint)
        or invocation.arguments != call.arguments
        or route.binding.effect_class is not call.effect_class
        or route.binding.recovery_mode != "external_reconcile"
        or route.external_action_id != core.delivery_id
        or route.execution.workspace != core.baseline.source.workspace
        or route.execution.policy.decision is not PolicyDecisionKind.REQUIRE_APPROVAL
        or route.resources != (product_git_delivery_resource(core),)
    ):
        raise ValueError("Git计划必须由原Route绑定完整Core及Review")


def product_git_delivery_plan_fingerprint(plan: ProductGitDeliveryPlan) -> str:
    """Artifact SHA 是完整 JSONL SHA；不与原始 Diff SHA 混为一谈。"""
    return canonical_digest(plan.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))
