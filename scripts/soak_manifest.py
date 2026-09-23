"""Soak Run Manifest的低敏领域合同与样本复核。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from harnessix.domain.models import ContractModel
from scripts.soak_artifact_proof import ARTIFACT_PROOF_FILENAME, SoakArtifactProof
from scripts.soak_context_proof import CONTEXT_PROOF_FILENAME, SoakContextProof
from scripts.soak_restart_proof import (
    RESTART_PROOF_FILENAME,
    SoakRestartProof,
    verify_restart_proof,
)
from scripts.soak_sample_file import SAMPLE_FILENAME, read_sample_file
from scripts.soak_samples import SCENARIO_METRICS, ScenarioId, SoakQuantiles, SoakSample
from scripts.soak_sdk_proof import SDK_PROOF_FILENAME, SoakSdkProof, verify_sdk_proof

MeasurementBoundary = Literal[
    "core_runtime",
    "app_service",
    "sdk_stdio",
    "artifact_store",
    "product_action",
    "product_startup",
]

SCENARIO_BOUNDARY: dict[str, str] = {
    "long_session": "core_runtime",
    "many_threads": "app_service",
    "sdk_capacity": "sdk_stdio",
    "artifact_growth": "artifact_store",
    "action_recovery": "product_action",
    "restart": "product_startup",
}


class SoakProviderEvidence(ContractModel):
    """只记录模型脚本身份和请求计数。"""

    mode: Literal["deterministic_stateless_v1", "product_no_turn_v1"]
    script_version: Literal["harnessix.soak-provider/v1", "harnessix.product-no-turn/v1"]
    request_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def matching_mode(self) -> Self:
        if self.mode == "product_no_turn_v1":
            if self.script_version != "harnessix.product-no-turn/v1" or self.request_count != 0:
                raise ValueError("无Turn产品证据不得记录模型请求")
        elif self.script_version != "harnessix.soak-provider/v1":
            raise ValueError("确定性模型脚本版本不匹配")
        return self


class SoakLoad(ContractModel):
    """记录实际负载规模，避免把计划数量冒充执行数量。"""

    turn_count: StrictInt = Field(ge=0)
    thread_count: StrictInt = Field(ge=0)
    artifact_count: StrictInt = Field(ge=0)
    pending_limit: StrictInt | None = Field(default=None, ge=1, le=1024)
    warmup_count: StrictInt = Field(ge=0)
    fault_matrix_version: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._/-]{0,63}$")


class SoakFileWatermarks(ContractModel):
    """开始和结束时的持久文件字节水位。"""

    db_before_bytes: StrictInt = Field(ge=0)
    db_after_bytes: StrictInt = Field(ge=0)
    wal_before_bytes: StrictInt = Field(ge=0)
    wal_after_bytes: StrictInt = Field(ge=0)
    artifact_before_bytes: StrictInt = Field(ge=0)
    artifact_after_bytes: StrictInt = Field(ge=0)


class SoakFaultCounts(ContractModel):
    """只保留固定故障分类的数量。"""

    cancelled: StrictInt = Field(ge=0)
    timed_out: StrictInt = Field(ge=0)
    eof: StrictInt = Field(ge=0)
    unknown_effect: StrictInt = Field(ge=0)
    duplicate_effect: StrictInt = Field(ge=0)
    orphan: StrictInt = Field(ge=0)


class SoakRssEvidence(ContractModel):
    """平台RSS单位来源及正式样本中的归一化峰值。"""

    source: Literal["getrusage", "proc_status", "GetProcessMemoryInfo"]
    raw_unit: Literal["bytes", "KiB"]
    normalization: Literal["identity", "kib_times_1024"]
    peak_bytes: StrictInt = Field(gt=0)
    unit_verified: StrictBool


class SoakProfileReference(ContractModel):
    """引用独立冻结的阈值Profile。"""

    profile_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SoakManifest(ContractModel):
    """单次Run的低敏事实索引；PASS只由后续独立验证报告给出。"""

    spec_version: Literal["harnessix.soak-manifest/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    scenario_version: Literal["harnessix.soak-scenario/v1"]
    scenario_id: ScenarioId
    measurement_boundary: MeasurementBoundary
    seed: StrictInt = Field(ge=0)
    provider: SoakProviderEvidence
    platform: Literal["linux", "macos", "windows"]
    python_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    cpu_count: StrictInt = Field(gt=0)
    physical_memory_bytes: StrictInt = Field(gt=0)
    hardware_class: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    started_at: datetime
    ended_at: datetime
    status: Literal["baseline", "failed", "unverified"]
    load: SoakLoad
    sample_counts: dict[str, StrictInt]
    quantile_method: Literal["nearest_rank_v1"]
    statistics: dict[str, SoakQuantiles]
    rss: SoakRssEvidence
    file_watermarks: SoakFileWatermarks
    fault_counts: SoakFaultCounts
    evidence_sha256: dict[str, str]
    threshold_profile_ref: SoakProfileReference | None

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        """核对场景边界、正式样本集合、平台RSS和负载门槛。"""

        if self.measurement_boundary != SCENARIO_BOUNDARY[self.scenario_id]:
            raise ValueError("场景测量边界不匹配")
        if (
            self.scenario_id == "artifact_growth"
            and self.spec_version != "harnessix.soak-manifest/v3"
        ):
            raise ValueError("Artifact增长场景必须携带v3证明")
        if self.scenario_id == "sdk_capacity" and self.spec_version != "harnessix.soak-manifest/v4":
            raise ValueError("SDK容量场景必须携带v4证明")
        if self.scenario_id == "restart" and self.spec_version != "harnessix.soak-manifest/v5":
            raise ValueError("产品重启场景必须携带v5证明")
        if (self.scenario_id == "restart") != (self.provider.mode == "product_no_turn_v1"):
            raise ValueError("重启场景必须使用无Turn产品Provider证据")
        if (
            self.started_at.utcoffset() != UTC.utcoffset(None)
            or self.ended_at.utcoffset() != UTC.utcoffset(None)
            or self.ended_at < self.started_at
        ):
            raise ValueError("运行时间必须为递增UTC时间")
        metrics = SCENARIO_METRICS[self.scenario_id]
        if set(self.sample_counts) != metrics or set(self.statistics) != metrics:
            raise ValueError("正式指标集合不完整")
        if any(type(value) is not int or value <= 0 for value in self.sample_counts.values()):
            raise ValueError("正式样本计数无效")
        if any(
            self.statistics[metric].sample_count != count
            for metric, count in self.sample_counts.items()
        ):
            raise ValueError("统计样本数与清单不一致")
        evidence_files = {SAMPLE_FILENAME}
        if self.spec_version == "harnessix.soak-manifest/v2":
            evidence_files.add(CONTEXT_PROOF_FILENAME)
        if self.spec_version == "harnessix.soak-manifest/v3":
            evidence_files.add(ARTIFACT_PROOF_FILENAME)
        if self.spec_version == "harnessix.soak-manifest/v4":
            evidence_files.add(SDK_PROOF_FILENAME)
        if self.spec_version == "harnessix.soak-manifest/v5":
            evidence_files.add(RESTART_PROOF_FILENAME)
        if set(self.evidence_sha256) != evidence_files or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.evidence_sha256.values()
        ):
            raise ValueError("样本文件摘要无效")
        if self.status == "baseline" and self.threshold_profile_ref is not None:
            raise ValueError("基线Run不得引用阈值Profile")
        if self.status == "baseline" and not self.rss.unit_verified:
            raise ValueError("RSS单位未验证不可发布基线")
        if self.status == "baseline" and (
            (self.scenario_id == "long_session" and self.load.turn_count < 1000)
            or (self.scenario_id == "many_threads" and self.load.thread_count < 500)
        ):
            raise ValueError("正式基线负载不足")
        if (
            self.status == "baseline"
            and self.scenario_id == "long_session"
            and (
                self.sample_counts["turn_local"] < 1000
                or self.provider.request_count < self.load.turn_count
            )
        ):
            raise ValueError("长会话样本或模型请求数不足")
        if (
            self.status == "baseline"
            and self.scenario_id == "many_threads"
            and (
                self.sample_counts["app_service_startup"] < 3
                or self.sample_counts["thread_list_page"] < 3
            )
        ):
            raise ValueError("多Thread启动或列表样本数不足")
        if self.scenario_id == "sdk_capacity" and self.load.pending_limit is None:
            raise ValueError("SDK场景缺少协商Pending上限")
        if self.scenario_id == "action_recovery" and self.load.fault_matrix_version is None:
            raise ValueError("Action场景缺少故障矩阵版本")
        if self.rss.source == "GetProcessMemoryInfo":
            if self.platform != "windows" or (
                self.rss.raw_unit,
                self.rss.normalization,
            ) != ("bytes", "identity"):
                raise ValueError("Windows RSS来源或单位不匹配")
        elif self.platform == "windows":
            raise ValueError("Windows RSS必须使用原生工作集接口")
        if self.rss.source == "proc_status" and self.platform != "linux":
            raise ValueError("proc_status RSS来源只适用于Linux")
        if self.platform == "linux" and (
            self.rss.raw_unit,
            self.rss.normalization,
        ) != ("KiB", "kib_times_1024"):
            raise ValueError("Linux RSS单位不匹配")
        return self


class SoakManifestV2(SoakManifest):
    """长会话Context证明版；v1序列化和历史证据保持原字节。"""

    spec_version: Literal["harnessix.soak-manifest/v2"]
    scenario_version: Literal["harnessix.soak-scenario/v2"]
    scenario_id: Literal["long_session"]
    summary_request_count: StrictInt = Field(ge=1)
    context_proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def proof_reference(self) -> Self:
        if self.context_proof_sha256 != self.evidence_sha256[CONTEXT_PROOF_FILENAME]:
            raise ValueError("Context证明摘要与证据索引不一致")
        return self


class SoakManifestV3(SoakManifest):
    """Artifact增长证明版；旧v1/v2证据不增字段。"""

    spec_version: Literal["harnessix.soak-manifest/v3"]
    scenario_version: Literal["harnessix.soak-scenario/v3"]
    scenario_id: Literal["artifact_growth"]
    artifact_proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def proof_reference(self) -> Self:
        if self.artifact_proof_sha256 != self.evidence_sha256[ARTIFACT_PROOF_FILENAME]:
            raise ValueError("Artifact证明摘要与证据索引不一致")
        if (
            self.load.artifact_count < self.load.warmup_count + 1
            or self.load.turn_count != self.load.artifact_count
            or self.load.thread_count != 1
        ):
            raise ValueError("Artifact负载必须每Turn发布一件并包含正式样本")
        if self.status == "baseline" and (
            self.load.warmup_count != 2 or self.load.artifact_count < self.load.warmup_count + 20
        ):
            raise ValueError("正式Artifact基线需要2件预热和至少20件正式样本")
        return self


class SoakManifestV4(SoakManifest):
    """SDK容量阶段证明版；v1～v3历史证据保持原字节。"""

    spec_version: Literal["harnessix.soak-manifest/v4"]
    scenario_version: Literal["harnessix.soak-scenario/v4"]
    scenario_id: Literal["sdk_capacity"]
    sdk_proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def proof_reference(self) -> Self:
        if self.sdk_proof_sha256 != self.evidence_sha256[SDK_PROOF_FILENAME]:
            raise ValueError("SDK证明摘要与证据索引不一致")
        if (
            self.load.pending_limit is None
            or self.load.pending_limit < 2
            or self.load.turn_count != 0
            or self.load.thread_count != 0
            or self.load.artifact_count != 0
            or self.provider.request_count != 0
            or self.load.fault_matrix_version != "sdk-capacity-v1"
            or self.fault_counts.cancelled <= self.load.warmup_count
            or any(
                value != 0
                for key, value in self.fault_counts.model_dump().items()
                if key != "cancelled"
            )
        ):
            raise ValueError("SDK容量场景负载或故障计数无效")
        if self.status == "baseline" and (
            self.load.pending_limit != 64
            or self.fault_counts.cancelled < 4
            or self.load.warmup_count != 1
            or self.sample_counts["sdk_roundtrip"] < 20
        ):
            raise ValueError("正式SDK基线需要协商64容量、一次预热及三轮正式负载")
        return self


class SoakManifestV5(SoakManifest):
    """完整产品重启阶段证明版；v1～v4的原始证据不改变。"""

    spec_version: Literal["harnessix.soak-manifest/v5"]
    scenario_version: Literal["harnessix.soak-scenario/v5"]
    scenario_id: Literal["restart"]
    restart_proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def proof_reference(self) -> Self:
        if self.restart_proof_sha256 != self.evidence_sha256[RESTART_PROOF_FILENAME]:
            raise ValueError("重启证明摘要与证据索引不一致")
        if (
            self.load.turn_count != 0
            or self.load.thread_count == 0
            or self.load.artifact_count != 0
            or self.load.pending_limit is not None
            or self.load.fault_matrix_version != "product-restart-v1"
            or self.load.warmup_count != 1
            or self.fault_counts.eof != 1
            or self.sample_counts["rss_peak"] != 1
            or self.file_watermarks.db_after_bytes == 0
            or self.file_watermarks.artifact_before_bytes != 0
            or self.file_watermarks.artifact_after_bytes != 0
            or any(
                value != 0 for key, value in self.fault_counts.model_dump().items() if key != "eof"
            )
        ):
            raise ValueError("产品重启负载或故障计数无效")
        if self.status == "baseline" and (
            self.load.thread_count < 500 or self.sample_counts["product_startup"] < 3
        ):
            raise ValueError("正式重启基线需要500 Thread和至少3次新进程启动")
        return self


def verify_restart_manifest(
    manifest: SoakManifestV5, proof: SoakRestartProof, samples: tuple[SoakSample, ...]
) -> None:
    """从正式样本与产品文件水位交叉核验完整产品重启证明。"""

    verify_restart_proof(
        proof,
        run_id=manifest.run_id,
        thread_count=manifest.load.thread_count,
        warmup_count=manifest.load.warmup_count,
        measured_restarts=manifest.sample_counts["product_startup"],
        fault_eof=manifest.fault_counts.eof,
        rss_peak_bytes=manifest.rss.peak_bytes,
        db_before_bytes=manifest.file_watermarks.db_before_bytes,
        db_after_bytes=manifest.file_watermarks.db_after_bytes,
        wal_before_bytes=manifest.file_watermarks.wal_before_bytes,
        wal_after_bytes=manifest.file_watermarks.wal_after_bytes,
        samples=samples,
    )


def verify_sdk_manifest(
    manifest: SoakManifestV4,
    proof: SoakSdkProof,
    samples: tuple[SoakSample, ...],
) -> None:
    """用原始样本核对Proof与Manifest的负载、故障和进程内存。"""

    assert manifest.load.pending_limit is not None
    verify_sdk_proof(
        proof,
        run_id=manifest.run_id,
        pending_limit=manifest.load.pending_limit,
        warmup_count=manifest.load.warmup_count,
        measured_rounds=manifest.fault_counts.cancelled - manifest.load.warmup_count,
        roundtrip_count=manifest.sample_counts["sdk_roundtrip"],
        cancelled_count=manifest.fault_counts.cancelled,
        rss_peak_bytes=manifest.rss.peak_bytes,
        samples=samples,
    )


def verify_artifact_manifest(manifest: SoakManifestV3, proof: SoakArtifactProof) -> None:
    """核对正式负载是否混合大小件，并绑定Provider请求计数。"""

    if manifest.provider.request_count != manifest.load.artifact_count * 2:
        raise ValueError("Artifact模型请求计数不一致")
    if manifest.status == "baseline":
        measured = [entry for entry in proof.entries if entry.phase == "measure"]
        if (
            sum(entry.size_class == "near_limit" for entry in measured) < 2
            or sum(entry.size_class == "small" for entry in measured) < 2
        ):
            raise ValueError("正式Artifact基线缺少混合大小件")


def verify_context_proof(manifest: SoakManifestV2, proof: SoakContextProof) -> None:
    """核对独立证明与负载、普通Provider及摘要Provider请求数。"""

    if (
        proof.run_id != manifest.run_id
        or len(proof.turns) != manifest.load.warmup_count + manifest.load.turn_count
        or any(
            turn.phase != ("warmup" if index < manifest.load.warmup_count else "measure")
            for index, turn in enumerate(proof.turns)
        )
        or proof.model_request_count != manifest.provider.request_count
        or proof.summary_request_count != manifest.summary_request_count
    ):
        raise ValueError("Context证明与Manifest负载或Provider账本不一致")


def verify_manifest_samples(manifest: SoakManifest, run_directory: Path) -> None:
    """从落盘样本独立重算摘要、统计和RSS，拒绝Manifest自报值。"""

    samples, digest, statistics = read_sample_file(
        run_directory,
        expected_sha256=manifest.evidence_sha256[SAMPLE_FILENAME],
        run_id=manifest.run_id,
        scenario_id=manifest.scenario_id,
        expected_measured=manifest.sample_counts,
    )
    rss_samples = [
        sample for sample in samples if sample.metric == "rss_peak" and sample.phase == "measure"
    ]
    if (
        digest != manifest.evidence_sha256[SAMPLE_FILENAME]
        or statistics != manifest.statistics
        or max(sample.value for sample in rss_samples) != manifest.rss.peak_bytes
        or any(
            sample.rss_source != manifest.rss.source
            or sample.rss_raw_unit != manifest.rss.raw_unit
            or sample.rss_normalization != manifest.rss.normalization
            for sample in rss_samples
        )
    ):
        raise ValueError("Soak Manifest与原始样本不一致")
