"""SDK stdio容量Soak的低敏阶段证明与独立数值复核。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from harnessix.domain.models import ContractModel
from scripts.soak_rss import RssObservation
from scripts.soak_samples import SoakSample

SDK_PROOF_FILENAME = "sdk-proof.json"
MAX_SDK_PROOF_BYTES = 128 * 1024


class SoakSdkChildResult(ContractModel):
    """子进程退出前写入私有夹具目录，不作为Run证据原件。"""

    spec_version: Literal["harnessix.soak-sdk-child/v1"]
    rss: RssObservation
    provider_request_count: Literal[0]


class SoakSdkRound(ContractModel):
    """一次满容量请求、取消、迟到响应与溢出请求的匿名资源快照。"""

    ordinal: StrictInt = Field(ge=1, le=100)
    phase: Literal["warmup", "measure"]
    pending_at_capacity: StrictInt = Field(ge=1, le=1024)
    pending_after_cancel: StrictInt = Field(ge=0, le=1024)
    abandoned_after_cancel: StrictInt = Field(ge=0, le=1024)
    pending_after_late: StrictInt = Field(ge=0, le=1024)
    abandoned_after_late: StrictInt = Field(ge=0, le=1024)
    pending_after_drain: StrictInt = Field(ge=0, le=1024)
    abandoned_after_drain: StrictInt = Field(ge=0, le=1024)
    overflow_entered_after_late: StrictBool
    successful_responses: StrictInt = Field(ge=0, le=1024)


class SoakSdkProof(ContractModel):
    """证明实测达到协商容量且取消墓碑没有被提前释放。"""

    spec_version: Literal["harnessix.soak-sdk-proof/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    negotiated_pending_limit: StrictInt = Field(ge=2, le=1024)
    rounds: tuple[SoakSdkRound, ...] = Field(min_length=1, max_length=100)
    roundtrip_sample_indices: tuple[StrictInt, ...] = Field(min_length=1, max_length=10000)
    roundtrip_window_ns: StrictInt = Field(gt=0)
    client_rss_bytes: StrictInt = Field(gt=0)
    server_rss_bytes: StrictInt = Field(gt=0)
    close_state: Literal["closed"]
    pending_after_close: Literal[0]
    abandoned_after_close: Literal[0]

    @model_validator(mode="after")
    def capacity_invariants(self) -> Self:
        limit = self.negotiated_pending_limit
        warmup = sum(item.phase == "warmup" for item in self.rounds)
        if any(
            item.ordinal != index
            or item.phase != ("warmup" if index <= warmup else "measure")
            or item.pending_at_capacity != limit
            or item.pending_after_cancel != limit - 1
            or item.abandoned_after_cancel != 1
            or item.pending_after_late != limit
            or item.abandoned_after_late != 0
            or item.pending_after_drain != 0
            or item.abandoned_after_drain != 0
            or not item.overflow_entered_after_late
            or item.successful_responses != limit
            for index, item in enumerate(self.rounds, start=1)
        ):
            raise ValueError("SDK容量、迟到响应或关闭前排空证明无效")
        if len(set(self.roundtrip_sample_indices)) != len(self.roundtrip_sample_indices):
            raise ValueError("SDK吞吐样本索引重复")
        return self


def verify_sdk_proof(
    proof: SoakSdkProof,
    *,
    run_id: str,
    pending_limit: int,
    warmup_count: int,
    measured_rounds: int,
    roundtrip_count: int,
    cancelled_count: int,
    rss_peak_bytes: int,
    samples: tuple[SoakSample, ...],
) -> None:
    """从原始样本集合、负载、故障计数和阶段证明重算覆盖关系。"""

    if (
        proof.run_id != run_id
        or proof.negotiated_pending_limit != pending_limit
        or len(proof.rounds) != warmup_count + measured_rounds
        or sum(item.phase == "warmup" for item in proof.rounds) != warmup_count
        or len(proof.roundtrip_sample_indices) != roundtrip_count
        or cancelled_count != len(proof.rounds)
        or rss_peak_bytes != max(proof.client_rss_bytes, proof.server_rss_bytes)
    ):
        raise ValueError("SDK证明与Manifest负载、故障或RSS不一致")
    roundtrips = tuple(
        sample
        for sample in samples
        if sample.metric == "sdk_roundtrip" and sample.phase == "measure"
    )
    if tuple(
        sample.sample_index for sample in roundtrips
    ) != proof.roundtrip_sample_indices or proof.roundtrip_window_ns < sum(
        sample.value for sample in roundtrips
    ):
        raise ValueError("SDK正式吞吐样本覆盖不完整")
