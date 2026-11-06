"""HTTP采集执行方式（未实现，见ADR 0120）。

逐条调用服务类系统并冻结原始响应，只产出试次记录，不判分。

Harbor执行方式见evalkit.harbor。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue

from evalkit.contracts import (
    Cost,
    DatasetItem,
    RunPlan,
    TrialRecord,
    TrialStatus,
    Usage,
)

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
