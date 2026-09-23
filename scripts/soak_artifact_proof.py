"""Artifact Soak的低敏逐件覆盖与清理证明合同。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, MAX_ARTIFACT_RECORDS
from harnessix.domain.models import ContractModel
from scripts.soak_samples import SoakSample

ARTIFACT_PROOF_FILENAME = "artifact-proof.json"
MAX_ARTIFACT_PROOF_BYTES = 256 * 1024


class SoakArtifactEntry(ContractModel):
    """匿名Artifact序号与完整分页样本索引；不保存正文或业务身份。"""

    ordinal: StrictInt = Field(ge=1, le=1000)
    phase: Literal["warmup", "measure"]
    size_class: Literal["small", "near_limit"]
    size_bytes: StrictInt = Field(gt=0, le=MAX_ARTIFACT_BYTES)
    record_count: StrictInt = Field(gt=0, le=MAX_ARTIFACT_RECORDS)
    read_record_count: StrictInt = Field(ge=0, le=MAX_ARTIFACT_RECORDS)
    page_count: StrictInt = Field(gt=0, le=MAX_ARTIFACT_RECORDS)
    publish_sample_index: StrictInt = Field(gt=0)
    read_sample_indices: tuple[StrictInt, ...] = Field(
        min_length=1, max_length=MAX_ARTIFACT_RECORDS
    )

    @model_validator(mode="after")
    def complete_read(self) -> Self:
        if self.read_record_count != self.record_count:
            raise ValueError("Artifact分页记录未完整读取")
        if self.page_count != len(self.read_sample_indices):
            raise ValueError("Artifact分页样本数量不一致")
        if self.size_class == "near_limit" and not (
            MAX_ARTIFACT_BYTES * 8 // 10 <= self.size_bytes < MAX_ARTIFACT_BYTES
        ):
            raise ValueError("近上限Artifact正文大小无效")
        if self.size_class == "small" and self.size_bytes >= MAX_ARTIFACT_BYTES * 8 // 10:
            raise ValueError("小件Artifact正文大小无效")
        return self


class SoakArtifactCleanup(ContractModel):
    """逻辑正文清理与保留Manifest的数值事实。"""

    before_body_bytes: StrictInt = Field(gt=0)
    after_body_bytes: StrictInt = Field(ge=0)
    expired_count: StrictInt = Field(gt=0)
    tombstone_count: StrictInt = Field(gt=0)
    manifest_count: StrictInt = Field(gt=0)
    protected_count: StrictInt = Field(ge=0)


class SoakArtifactProof(ContractModel):
    """Reader可以从原始样本独立核对逐件采样覆盖的数值账本。"""

    spec_version: Literal["harnessix.soak-artifact-proof/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    entries: tuple[SoakArtifactEntry, ...] = Field(min_length=1, max_length=1000)
    cleanup: SoakArtifactCleanup

    @model_validator(mode="after")
    def complete_cleanup(self) -> Self:
        count = len(self.entries)
        warmup_count = sum(entry.phase == "warmup" for entry in self.entries)
        if any(entry.ordinal != index for index, entry in enumerate(self.entries, start=1)):
            raise ValueError("Artifact序号不连续")
        if any(
            entry.phase != ("warmup" if index < warmup_count else "measure")
            for index, entry in enumerate(self.entries)
        ):
            raise ValueError("Artifact预热与正式阶段顺序无效")
        if (
            self.cleanup.before_body_bytes != sum(entry.size_bytes for entry in self.entries)
            or self.cleanup.after_body_bytes != 0
            or self.cleanup.expired_count != count
            or self.cleanup.tombstone_count != count
            or self.cleanup.manifest_count != count
            or self.cleanup.protected_count != 0
        ):
            raise ValueError("Artifact清理或Manifest数量不一致")
        return self


def verify_artifact_proof(
    proof: SoakArtifactProof,
    *,
    run_id: str,
    artifact_count: int,
    warmup_count: int,
    samples: tuple[SoakSample, ...],
    artifact_before_bytes: int,
    artifact_after_bytes: int,
) -> None:
    """拒绝重复、漏记或跨指标复用样本；清理前水位不被零值掩盖。"""

    if (
        proof.run_id != run_id
        or len(proof.entries) != artifact_count
        or sum(entry.phase == "warmup" for entry in proof.entries) != warmup_count
        or artifact_before_bytes != 0
        or artifact_after_bytes != proof.cleanup.before_body_bytes
    ):
        raise ValueError("Artifact证明与负载或字节水位不一致")
    by_index = {sample.sample_index: sample for sample in samples}
    used: set[int] = set()
    last_index = 0
    for entry in proof.entries:
        indices = (entry.publish_sample_index, *entry.read_sample_indices)
        if (
            len(set(indices)) != len(indices)
            or used.intersection(indices)
            or entry.publish_sample_index <= last_index
            or any(left >= right for left, right in zip(indices, indices[1:], strict=False))
        ):
            raise ValueError("Artifact样本索引重复")
        used.update(indices)
        last_index = indices[-1]
        publish = by_index.get(entry.publish_sample_index)
        if publish is None or publish.metric != "artifact_publish" or publish.phase != entry.phase:
            raise ValueError("Artifact发布样本不匹配")
        if any(
            by_index.get(index) is None
            or by_index[index].metric != "artifact_read"
            or by_index[index].phase != entry.phase
            for index in entry.read_sample_indices
        ):
            raise ValueError("Artifact读取样本不匹配")
    expected = {
        sample.sample_index
        for sample in samples
        if sample.metric in {"artifact_publish", "artifact_read"}
    }
    if used != expected:
        raise ValueError("Artifact发布或分页样本覆盖不完整")
