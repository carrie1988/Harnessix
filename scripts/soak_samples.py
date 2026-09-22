"""0.9.3d Soak低敏原始样本与可复算统计合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from harnessix.domain.models import ContractModel

ScenarioId = Literal[
    "long_session",
    "many_threads",
    "sdk_capacity",
    "artifact_growth",
    "action_recovery",
    "restart",
]
Metric = Literal[
    "turn_local",
    "thread_list_page",
    "sdk_roundtrip",
    "artifact_publish",
    "artifact_read",
    "recovery_scan",
    "product_startup",
    "rss_peak",
]
SCENARIO_METRICS: dict[str, frozenset[str]] = {
    "long_session": frozenset({"turn_local", "rss_peak"}),
    "many_threads": frozenset({"thread_list_page", "rss_peak"}),
    "sdk_capacity": frozenset({"sdk_roundtrip", "rss_peak"}),
    "artifact_growth": frozenset({"artifact_publish", "artifact_read", "rss_peak"}),
    "action_recovery": frozenset({"recovery_scan", "rss_peak"}),
    "restart": frozenset({"product_startup", "rss_peak"}),
}


class SoakSample(ContractModel):
    """只记录数值、场景、单位及RSS归一化依据的单个样本。"""

    spec_version: Literal["harnessix.soak-sample/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    scenario_id: ScenarioId
    sample_index: StrictInt = Field(gt=0)
    phase: Literal["warmup", "measure"]
    metric: Metric
    value: StrictInt = Field(ge=0)
    unit: Literal["ns", "bytes"]
    clock: Literal["monotonic_ns"] | None = None
    rss_source: Literal["getrusage", "GetProcessMemoryInfo"] | None = None
    rss_raw_unit: Literal["bytes", "KiB"] | None = None
    rss_normalization: Literal["identity", "kib_times_1024"] | None = None
    rss_raw_value: StrictInt | None = Field(default=None, gt=0)
    rss_bytes: StrictInt | None = Field(default=None, gt=0)
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_measurement(self) -> Self:
        """强制指标单位、测量时钟和RSS原始值互相吻合。"""

        if self.metric not in SCENARIO_METRICS[self.scenario_id]:
            raise ValueError("场景不允许此指标")
        if self.observed_at is not None and self.observed_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError("样本时间必须为UTC")
        if self.metric != "rss_peak":
            if self.unit != "ns" or self.clock != "monotonic_ns":
                raise ValueError("时延指标必须使用monotonic_ns和ns")
            if any(
                value is not None
                for value in (
                    self.rss_source,
                    self.rss_raw_unit,
                    self.rss_normalization,
                    self.rss_raw_value,
                    self.rss_bytes,
                )
            ):
                raise ValueError("非RSS样本不得携带RSS字段")
            return self
        if self.unit != "bytes" or self.clock is not None:
            raise ValueError("RSS样本必须使用bytes且不使用时延时钟")
        if any(
            value is None
            for value in (
                self.rss_source,
                self.rss_raw_unit,
                self.rss_normalization,
                self.rss_raw_value,
                self.rss_bytes,
            )
        ):
            raise ValueError("RSS样本缺少原始测量或归一化依据")
        assert self.rss_raw_value is not None and self.rss_bytes is not None
        if (self.rss_raw_unit, self.rss_normalization) == ("bytes", "identity"):
            expected = self.rss_raw_value
        elif (self.rss_raw_unit, self.rss_normalization) == ("KiB", "kib_times_1024"):
            expected = self.rss_raw_value * 1024
        else:
            raise ValueError("RSS原始单位与归一化规则不匹配")
        if self.value != self.rss_bytes or self.rss_bytes != expected:
            raise ValueError("RSS归一化值与原始测量不匹配")
        return self


class SoakQuantiles(ContractModel):
    """由正式样本重算的最近秩整数分位数。"""

    sample_count: StrictInt = Field(gt=0)
    p50: StrictInt = Field(ge=0)
    p95: StrictInt = Field(ge=0)
    p99: StrictInt = Field(ge=0)


def nearest_rank(values: tuple[int, ...]) -> SoakQuantiles:
    """使用ceil(p*n/100)的1基位置；不进行浮点插值。"""

    if not values or any(type(value) is not int or value < 0 for value in values):
        raise ValueError("分位数样本必须是非空非负整数集合")
    ordered = sorted(values)
    count = len(ordered)

    def select(percentile: int) -> int:
        return ordered[(percentile * count + 99) // 100 - 1]

    return SoakQuantiles(
        sample_count=count,
        p50=select(50),
        p95=select(95),
        p99=select(99),
    )


def validate_sample_series(
    samples: tuple[SoakSample, ...],
    *,
    run_id: str,
    scenario_id: ScenarioId,
    expected_measured: dict[str, int],
) -> dict[str, SoakQuantiles]:
    """校验全Run连续序号和固定指标计数，只统计正式阶段。"""

    if not samples or set(expected_measured) != SCENARIO_METRICS[scenario_id]:
        raise ValueError("场景指标集合不完整")
    if any(type(count) is not int or count <= 0 for count in expected_measured.values()):
        raise ValueError("正式样本计数必须为正整数")
    measured: dict[str, list[int]] = {metric: [] for metric in expected_measured}
    for index, sample in enumerate(samples, start=1):
        if (
            sample.sample_index != index
            or sample.run_id != run_id
            or sample.scenario_id != scenario_id
        ):
            raise ValueError("样本序号或运行身份不连续")
        if sample.phase == "measure":
            measured[sample.metric].append(sample.value)
    if any(len(measured[metric]) != count for metric, count in expected_measured.items()):
        raise ValueError("正式样本数与冻结计划不一致")
    return {metric: nearest_rank(tuple(values)) for metric, values in measured.items()}
