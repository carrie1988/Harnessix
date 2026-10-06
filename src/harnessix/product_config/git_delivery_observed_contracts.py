"""完整用户观察进入正式 Core2/Plan2；旧代际不补字段或自动升级。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, GetJsonSchemaHandler, ValidationInfo, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from harnessix.agent.models import ToolCallContent
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.delivery.contracts import MAX_WORKSPACE_DIFF_BYTES, DeliveryContract
from harnessix.delivery.git_contracts import GitCommitSpec
from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.product_config.git_delivery_plan_contracts import (
    GitIndexFileObservation,
    ProductGitWorktreeIntent,
    _scope_schema,
    product_git_delivery_plan_fingerprint,
    validate_product_git_delivery_route,
)
from harnessix.product_config.git_parent_contracts import ProductGitDeliveryBaselineV2
from harnessix.product_config.git_user_observation_contracts import (
    ProductGitUserObservation,
    validate_product_git_user_observation,
)
from harnessix.tools.contracts import Revision
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2


class ProductGitDeliveryCoreV2(DeliveryContract):
    """U观察只序列化一次；只读视图让原唯一算法消费同一完整基准。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        ser_json_bytes="hex",
        val_json_bytes="hex",
    )
    spec_version: Literal["harnessix.product-git-delivery-core/v2"] = (
        "harnessix.product-git-delivery-core/v2"
    )
    delivery_id: UUID
    store_id: UUID
    key_id: UUID
    thread_id: UUID
    turn_id: UUID
    call: ToolCallContent
    user_observation: ProductGitUserObservation
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
        """沿原完整Scope/hex声明算法，旧Core1 Schema保持原字节。"""
        result = handler(schema)
        root = handler.resolve_ref_schema(result)
        _scope_schema(root["properties"]["object_scope"], handler, set())
        return result

    @property
    def baseline(self) -> ProductGitDeliveryBaselineV2:
        """原基准来自唯一完整观察，不允许第二份基准分叉。"""
        return self.user_observation.baseline

    @property
    def common_directory_path_sha256(self) -> str:
        """原算法的兼容只读视图；不增加持久字段或别名。"""
        return self.user_observation.common_directory_path_sha256

    @property
    def common_directory_identity(self) -> str:
        """common身份不能与完整用户观察分别声明。"""
        return self.user_observation.common_directory_identity

    @property
    def index_file_observation(self) -> GitIndexFileObservation:
        """物理Index由原观察完整绑定，不复制或覆盖用户Index。"""
        return self.user_observation.index_file_observation

    @model_validator(mode="after")
    def complete_core(self, info: ValidationInfo) -> Self:
        """全观察及原唯一跨字段算法共同验真；数据合同不认证会话或授予批准。"""
        from harnessix.product_config.git_delivery_plan_validation import (
            validate_product_git_delivery_core,
        )

        if type(self.user_observation) is not ProductGitUserObservation or (
            self.store_id,
            self.key_id,
        ) != (self.user_observation.store_id, self.user_observation.key_id):
            raise ValueError("Git Core2必须绑定同一原Session身份的完整用户观察")
        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        checkpoint()
        validate_product_git_user_observation(self.user_observation)
        validate_product_git_delivery_core(self, checkpoint)
        return self


class ProductGitDeliveryPlanV2(DeliveryContract):
    """Core2与原Route2/完整Artifact构成新封套；旧Plan1不接受新Core。"""

    spec_version: Literal["harnessix.product-git-delivery-plan/v2"] = (
        "harnessix.product-git-delivery-plan/v2"
    )
    core: ProductGitDeliveryCoreV2
    route: ActionRoutePlanV2
    review_artifact: ArtifactRef
    fingerprint: Revision

    @model_validator(mode="after")
    def bound_envelope(self) -> Self:
        """实际代际与原交叉字段不可降级；Artifact摘要仍是完整JSONL摘要。"""
        if type(self.core) is not ProductGitDeliveryCoreV2:
            raise ValueError("Git Plan2必须包含完整Core2")
        validate_product_git_delivery_route(self.core, self.route)
        if (
            not self.review_artifact.complete
            or self.review_artifact.records < 1
            or self.review_artifact.size_bytes < 1
            or self.fingerprint != product_git_delivery_plan_fingerprint(self)
        ):
            raise ValueError("Git计划必须由原Route绑定完整Core及Review")
        return self
