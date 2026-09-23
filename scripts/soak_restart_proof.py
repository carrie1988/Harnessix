"""完整产品重启Soak的低敏阶段证明及独立交叉核对。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from harnessix.domain.models import ContractModel
from harnessix.product_config.action_contracts import ProductActionStartupRecoveryReport
from harnessix.trusted_actions.recovery_contracts import ActionRecoveryScanReport
from scripts.soak_samples import SoakSample

RESTART_PROOF_FILENAME = "restart-proof.json"
MAX_RESTART_PROOF_BYTES = 128 * 1024

# 固定当前产品组合根的SQLite文件，不把宿主路径写入证据。
PRODUCT_DB_FILES = frozenset(
    {
        "product-config.db",
        "sessions.db",
        "execution-plans.db",
        "action-audit.db",
        "workspace-leases.db",
        "workspace-transactions/transactions.db",
    }
)


class SoakRestartCycle(ContractModel):
    """一次独立产品进程的握手、恢复、列表与退出事实。"""

    ordinal: StrictInt = Field(ge=1, le=100)
    phase: Literal["warmup", "crash", "measure"]
    startup_sample_index: StrictInt | None = Field(default=None, gt=0)
    thread_count: StrictInt = Field(ge=1, le=100_000)
    thread_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_generation: StrictInt = Field(ge=1)
    recovery_scan: ActionRecoveryScanReport
    recovery_report: ProductActionStartupRecoveryReport
    close_state: Literal["closed", "hard_exit"]

    @model_validator(mode="after")
    def consistent_cycle(self) -> Self:
        if self.owner_generation != self.recovery_scan.owner_generation:
            raise ValueError("重启Owner代际与持久扫描报告不一致")
        if (self.phase == "crash") != (self.startup_sample_index is None):
            raise ValueError("重启周期的正式样本索引无效")
        if (self.phase == "crash") != (self.close_state == "hard_exit"):
            raise ValueError("只有受控崩溃周期可以硬退出")
        if self.recovery_scan.scanned_routes or self.recovery_report.scanned_routes:
            raise ValueError("无Turn产品重启不得存在Action Route")
        if any(
            value != 0
            for field, value in self.recovery_scan.model_dump().items()
            if field not in {"spec_version", "owner_generation", "created_at", "report_sha256"}
        ) or any(
            value != 0
            for field, value in self.recovery_report.model_dump().items()
            if field
            not in {
                "spec_version",
                "candidate_config_sha256",
                "recovery_config_sha256",
                "created_at",
                "report_sha256",
            }
        ):
            raise ValueError("产品重启发现未解决的持久恢复问题")
        return self


class SoakRestartProof(ContractModel):
    """冻结同一State的完整Thread集合和有序进程恢复证据。"""

    spec_version: Literal["harnessix.soak-restart-proof/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    thread_count: StrictInt = Field(ge=1, le=100_000)
    thread_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cycles: tuple[SoakRestartCycle, ...] = Field(min_length=5, max_length=100)
    hard_exit_ack: StrictBool
    hard_exit_eof: StrictBool
    runner_rss_bytes: StrictInt = Field(gt=0)
    server_rss_bytes: StrictInt = Field(gt=0)
    db_before_bytes_by_name: dict[str, StrictInt]
    db_after_bytes_by_name: dict[str, StrictInt]
    wal_before_bytes_by_name: dict[str, StrictInt]
    wal_after_bytes_by_name: dict[str, StrictInt]

    @model_validator(mode="after")
    def consistent_proof(self) -> Self:
        if not self.hard_exit_ack or not self.hard_exit_eof:
            raise ValueError("受控硬退出缺少ACK或stdio EOF")
        if any(
            set(watermarks) != PRODUCT_DB_FILES or any(value < 0 for value in watermarks.values())
            for watermarks in (
                self.db_before_bytes_by_name,
                self.db_after_bytes_by_name,
                self.wal_before_bytes_by_name,
                self.wal_after_bytes_by_name,
            )
        ):
            raise ValueError("产品SQLite水位文件集合不完整")
        if any(
            cycle.ordinal != index
            or cycle.phase != ("warmup" if index == 1 else "crash" if index == 2 else "measure")
            or cycle.thread_count != self.thread_count
            or cycle.thread_set_sha256 != self.thread_set_sha256
            or (index > 1 and cycle.owner_generation <= self.cycles[index - 2].owner_generation)
            for index, cycle in enumerate(self.cycles, start=1)
        ):
            raise ValueError("重启周期、Thread集合或Owner代际不连续")
        return self


def verify_restart_proof(
    proof: SoakRestartProof,
    *,
    run_id: str,
    thread_count: int,
    warmup_count: int,
    measured_restarts: int,
    fault_eof: int,
    rss_peak_bytes: int,
    db_before_bytes: int,
    db_after_bytes: int,
    wal_before_bytes: int,
    wal_after_bytes: int,
    samples: tuple[SoakSample, ...],
) -> None:
    """由原始样本与聚合水位核对Proof，而非信任发布者的结论。"""

    startup_samples = tuple(sample for sample in samples if sample.metric == "product_startup")
    measured = tuple(sample for sample in startup_samples if sample.phase == "measure")
    if (
        proof.run_id != run_id
        or proof.thread_count != thread_count
        or warmup_count != 1
        or len(proof.cycles) != warmup_count + 1 + measured_restarts
        or fault_eof != 1
        or len(measured) != measured_restarts
        or rss_peak_bytes != max(proof.runner_rss_bytes, proof.server_rss_bytes)
        or db_before_bytes != sum(proof.db_before_bytes_by_name.values())
        or db_after_bytes != sum(proof.db_after_bytes_by_name.values())
        or wal_before_bytes != sum(proof.wal_before_bytes_by_name.values())
        or wal_after_bytes != sum(proof.wal_after_bytes_by_name.values())
    ):
        raise ValueError("重启证明与Manifest负载、故障、RSS或文件水位不一致")
    expected = tuple(cycle.startup_sample_index for cycle in proof.cycles if cycle.phase != "crash")
    covered_cycles = tuple(cycle for cycle in proof.cycles if cycle.phase != "crash")
    if expected != tuple(sample.sample_index for sample in startup_samples) or any(
        sample.phase != cycle.phase
        for sample, cycle in zip(startup_samples, covered_cycles, strict=True)
    ):
        raise ValueError("重启证明未覆盖预热和正式启动样本")
