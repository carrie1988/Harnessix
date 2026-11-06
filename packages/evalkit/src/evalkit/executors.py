"""两种执行方式的入口：只产出试次记录和冻结制品，不判分。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue

from evalkit.contracts import (
    ArtifactRef,
    Cost,
    DatasetItem,
    RunPlan,
    TrialRecord,
    TrialStatus,
    Usage,
)

# ---------------------------------------------------------------- Harbor


@dataclass(frozen=True)
class AgentResult:
    """智能体专属解析结果；通用字段进入试次，专属字段只进入system_refs。"""

    status: TrialStatus
    reason: str
    usage: Usage
    cost: Cost
    artifacts: tuple[ArtifactRef, ...]
    system_refs: Mapping[str, str]


class HarborResultParser(Protocol):
    """按Harbor智能体名称选择；Harnessix、mini-swe-agent等各自实现。"""

    agent_name: str

    def parse(self, trial_dir: Path, result: Mapping[str, JsonValue]) -> AgentResult: ...


def import_harbor_job(
    job_dir: Path,
    plan: RunPlan,
    arm_id: str,
    parsers: Mapping[str, HarborResultParser],
    evidence_root: Path,
) -> tuple[TrialRecord, ...]:
    """把一个Harbor Job目录导入为该组别的全部槽位；缺失结果占位为missing。"""
    raise NotImplementedError("迁移自harnessix.evals.harbor_import（ADR 0120 第4节）")


# ---------------------------------------------------------------- HTTP采集


@dataclass(frozen=True)
class CollectedResponse:
    status: TrialStatus
    reason: str
    body: bytes
    usage: Usage
    cost: Cost
    system_refs: Mapping[str, str]


class Collector(Protocol):
    """服务类系统的一次调用；认证由实现方持有，凭据不进入试次。"""

    def call(self, item: DatasetItem, request: JsonValue) -> CollectedResponse: ...


def collect(
    plan: RunPlan,
    arm_id: str,
    requests: Mapping[str, JsonValue],
    collector: Collector,
    evidence_root: Path,
) -> tuple[TrialRecord, ...]:
    """逐条调用并冻结原始响应；异常停批时未执行的槽位保留为missing。"""
    raise NotImplementedError("HTTP采集执行（ADR 0120 决策3）")


def import_frozen_responses(
    plan: RunPlan, arm_id: str, frozen_dir: Path, evidence_root: Path
) -> tuple[TrialRecord, ...]:
    """导入已冻结的响应快照（含清单摘要），不调用被测系统。"""
    raise NotImplementedError("冻结快照导入（ADR 0120 第6节通用性验证）")
