"""用户 Git 根的完整只读观察；与要求干净来源的原 RepositoryBinding 分离。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from harnessix.delivery.contracts import DeliveryContract
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_delivery_plan_contracts import GitIndexFileObservation
from harnessix.product_config.git_parent_contracts import ProductGitDeliveryBaselineV2
from harnessix.tools.contracts import Revision


class ProductGitUserObservation(DeliveryContract):
    """完整 Source2、基准及物理目录/Index 事实；摘要不证明 Session 或批准。"""

    spec_version: Literal["harnessix.product-git-user-observation/v1"] = (
        "harnessix.product-git-user-observation/v1"
    )
    store_id: UUID
    key_id: UUID
    baseline: ProductGitDeliveryBaselineV2
    common_directory_path_sha256: Revision
    common_directory_identity: Revision
    git_directory_path_sha256: Revision
    git_directory_identity: Revision
    index_file_observation: GitIndexFileObservation
    config_sha256: Revision
    implementation_digest: Revision
    fingerprint: Revision

    @model_validator(mode="after")
    def complete_observation(self) -> Self:
        """保持新来源代际与完整指纹，不能用名称摘要替代配置值观察。"""
        validate_product_git_user_observation(self)
        return self


def validate_product_git_user_observation(value: ProductGitUserObservation) -> None:
    """模型与正式Core消费共同复用唯一完整观察指纹算法，不产生认证或批准。"""
    if type(value.baseline) is not ProductGitDeliveryBaselineV2:
        raise ValueError("Git用户观察必须包含完整新代际基准")
    if value.fingerprint != product_git_user_observation_fingerprint(value):
        raise ValueError("Git用户观察完整指纹不一致")


def product_git_user_observation_fingerprint(value: ProductGitUserObservation) -> str:
    """绑定全部事实及实现，不包括自身摘要，不创建认证或执行授权。"""
    return canonical_digest(
        value.model_dump(mode="json", exclude={"fingerprint"}, warnings="error")
    )


def build_product_git_user_observation(
    baseline: ProductGitDeliveryBaselineV2,
    facts: dict[str, str],
    index: GitIndexFileObservation,
    config: str,
    implementation: str,
    store_id: UUID,
    key_id: UUID,
) -> ProductGitUserObservation:
    """只组装完整已读事实；原 Session身份元数据不被转换为新的MAC或批准。"""
    candidate = ProductGitUserObservation.model_construct(
        store_id=store_id,
        key_id=key_id,
        baseline=baseline,
        common_directory_path_sha256=facts["common_directory_path_sha256"],
        common_directory_identity=facts["common_directory_identity"],
        git_directory_path_sha256=facts["git_directory_path_sha256"],
        git_directory_identity=facts["git_directory_identity"],
        index_file_observation=index,
        config_sha256=config,
        implementation_digest=implementation,
        fingerprint="0" * 64,
    )
    return ProductGitUserObservation(
        **candidate.model_dump(exclude={"fingerprint"}),
        fingerprint=product_git_user_observation_fingerprint(candidate),
    )
