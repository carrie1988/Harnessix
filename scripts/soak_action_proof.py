"""Action恢复Soak的低敏故障矩阵与对账证明合同。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from harnessix.domain.models import ContractModel
from scripts.soak_samples import SoakSample

ACTION_PROOF_FILENAME = "action-proof.json"
MAX_ACTION_PROOF_BYTES = 256 * 1024

_FAULT_KINDS = ("unknown_outcome", "host_crash", "plan_orphan", "artifact_orphan")


class SoakActionFault(ContractModel):
    """单个受控故障的低敏结果；不保存Plan/Route身份、路径或参数正文。"""

    kind: Literal["unknown_outcome", "host_crash", "plan_orphan", "artifact_orphan"]
    execute_calls: StrictInt = Field(ge=0, le=1)
    reconcile_calls: StrictInt = Field(ge=0, le=1)
    terminal_state: Literal["succeeded", "none"]

    @model_validator(mode="after")
    def fault_shape(self) -> Self:
        if self.kind in {"unknown_outcome", "host_crash"}:
            if (
                self.execute_calls != 1
                or self.reconcile_calls != 1
                or self.terminal_state != "succeeded"
            ):
                raise ValueError("UNKNOWN类故障必须一次执行、一次对账并收敛为succeeded")
        elif self.execute_calls != 0 or self.reconcile_calls != 0 or self.terminal_state != "none":
            raise ValueError("孤儿类故障不得产生执行或对账调用")
        return self


class SoakActionCycle(ContractModel):
    """一轮固定四故障与一次计时恢复扫描的低敏索引。"""

    ordinal: StrictInt = Field(ge=1, le=1000)
    phase: Literal["warmup", "measure"]
    faults: tuple[SoakActionFault, ...] = Field(min_length=4, max_length=4)
    owner_generation: StrictInt = Field(ge=1)
    scanned_routes: StrictInt = Field(ge=0)
    repaired_execution_plans: StrictInt = Field(ge=0)
    artifact_orphans: StrictInt = Field(ge=0)
    scan_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_sample_index: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def fixed_matrix(self) -> Self:
        if tuple(fault.kind for fault in self.faults) != _FAULT_KINDS:
            raise ValueError("Action故障矩阵顺序无效")
        if self.scanned_routes != self.ordinal * 2:
            raise ValueError("Action扫描Route计数与轮次不一致")
        if self.repaired_execution_plans != self.ordinal:
            raise ValueError("Action修复计数与轮次不一致")
        if self.artifact_orphans != self.ordinal:
            raise ValueError("Action孤儿计数与轮次不一致")
        return self


class SoakActionProof(ContractModel):
    """Reader可独立核对全Run故障矩阵、对账计数与扫描覆盖的数值账本。"""

    spec_version: Literal["harnessix.soak-action-proof/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    cycles: tuple[SoakActionCycle, ...] = Field(min_length=1, max_length=1000)
    unknown_resolved: StrictInt = Field(ge=2)
    duplicate_effects: StrictInt = Field(ge=0, le=0)
    crash_exits: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def complete_matrix(self) -> Self:
        count = len(self.cycles)
        warmup_count = sum(cycle.phase == "warmup" for cycle in self.cycles)
        if any(cycle.ordinal != index for index, cycle in enumerate(self.cycles, start=1)):
            raise ValueError("Action轮序号不连续")
        if any(
            cycle.phase != ("warmup" if index < warmup_count else "measure")
            for index, cycle in enumerate(self.cycles)
        ):
            raise ValueError("Action预热与正式阶段顺序无效")
        generations = [cycle.owner_generation for cycle in self.cycles]
        if any(left >= right for left, right in zip(generations, generations[1:], strict=False)):
            raise ValueError("Action Owner代际未严格递增")
        if self.unknown_resolved != count * 2:
            raise ValueError("Action UNKNOWN收敛计数不一致")
        if self.crash_exits != count:
            raise ValueError("Action崩溃退出计数不一致")
        return self


def verify_action_proof(
    proof: SoakActionProof,
    *,
    run_id: str,
    cycle_count: int,
    warmup_count: int,
    unknown_effect: int,
    samples: tuple[SoakSample, ...],
) -> None:
    """拒绝漏轮、计数漂移或跨指标复用样本；扫描样本索引必须唯一且递增。"""

    if (
        proof.run_id != run_id
        or len(proof.cycles) != cycle_count + warmup_count
        or sum(cycle.phase == "warmup" for cycle in proof.cycles) != warmup_count
        or proof.unknown_resolved != unknown_effect
    ):
        raise ValueError("Action证明与负载或故障计数不一致")
    by_index = {sample.sample_index: sample for sample in samples}
    used: set[int] = set()
    last_index = 0
    for cycle in proof.cycles:
        index = cycle.scan_sample_index
        if index in used or index <= last_index:
            raise ValueError("Action扫描样本索引重复")
        sample = by_index.get(index)
        if sample is None or sample.metric != "recovery_scan" or sample.phase != cycle.phase:
            raise ValueError("Action扫描样本不匹配")
        used.add(index)
        last_index = index
    expected = {sample.sample_index for sample in samples if sample.metric == "recovery_scan"}
    if used != expected:
        raise ValueError("Action扫描样本覆盖不完整")
