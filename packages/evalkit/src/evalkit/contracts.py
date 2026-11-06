"""系统无关的四个评测证据合同：数据集清单、运行计划、试次记录、运行报告。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from evalkit.digest import content_digest

Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Completeness = Literal["complete", "partial", "unknown"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


# ---------------------------------------------------------------- 数据集清单


class DatasetItem(Contract):
    """一个评测条目的身份；正文由系统侧冻结保存，清单只保留摘要和标签。"""

    item_id: Identifier
    item_digest: Digest
    labels: tuple[Identifier, ...] = Field(default=(), max_length=32)


class DatasetManifest(Contract):
    spec_version: Literal["evalkit.dataset/v1"] = "evalkit.dataset/v1"
    name: Identifier
    version: Identifier
    items: tuple[DatasetItem, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def items_are_unique(self) -> Self:
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("数据集条目ID重复")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self)


# ---------------------------------------------------------------- 运行计划


class ArmSpec(Contract):
    """一个组别：被测系统、版本与配置摘要，以及产出试次的执行方式。"""

    arm_id: Identifier
    system: Identifier
    system_version: ShortText
    config_digest: Digest
    executor: Literal["harbor", "http_collect", "import"]


class GraderSpec(Contract):
    """判分器身份；每个判分器至少产出一个布尔分数作为通过判定。"""

    grader_id: Identifier
    version: ShortText
    pass_score: Identifier = Field(description="该判分器输出的布尔通过分数名称")


class RunPlan(Contract):
    """预注册内容：执行前冻结，之后的报告必须绑定其摘要。"""

    spec_version: Literal["evalkit.run-plan/v1"] = "evalkit.run-plan/v1"
    run_name: Identifier
    dataset: DatasetManifest
    arms: tuple[ArmSpec, ...] = Field(min_length=1, max_length=16)
    attempts_per_item: int = Field(ge=1, le=16)
    graders: tuple[GraderSpec, ...] = Field(min_length=1, max_length=8)
    primary_grader: Identifier
    content_policy: Literal["metadata", "full"] = "metadata"
    created_at: AwareDatetime

    @model_validator(mode="after")
    def references_resolve(self) -> Self:
        if len({arm.arm_id for arm in self.arms}) != len(self.arms):
            raise ValueError("组别ID重复")
        grader_ids = [grader.grader_id for grader in self.graders]
        if len(set(grader_ids)) != len(grader_ids):
            raise ValueError("判分器ID重复")
        if self.primary_grader not in grader_ids:
            raise ValueError("主指标判分器不在计划内")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self)

    def slots(self) -> tuple[tuple[str, str, int], ...]:
        """计划的完整分母：组别 × 条目 × 重复，顺序固定。"""
        return tuple(
            (arm.arm_id, item.item_id, attempt)
            for arm in self.arms
            for item in self.dataset.items
            for attempt in range(self.attempts_per_item)
        )


# ---------------------------------------------------------------- 试次记录


class Score(Contract):
    name: Identifier
    value: bool | float | str
    grader_id: Identifier


class Usage(Contract):
    completeness: Completeness = "unknown"
    model_calls: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class Cost(Contract):
    """原币种成本；未知就是未知，不补零、不换算。"""

    completeness: Completeness = "unknown"
    # 与harnessix同序：typing的Union缓存把顺序不同但相等的Literal视为同一键，
    # 顺序不一致会让同进程内后生成的JSON Schema枚举顺序漂移。
    currency: Literal["USD", "CNY"] | None = None
    amount: Annotated[str, StringConstraints(pattern=r"^\d{1,9}(\.\d{1,9})?$")] | None = None

    @model_validator(mode="after")
    def amount_has_currency(self) -> Self:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("成本金额与币种必须同时存在")
        if self.completeness == "unknown" and self.amount is not None:
            raise ValueError("未知成本不能携带金额")
        return self


class ArtifactRef(Contract):
    """冻结制品（补丁、原始响应、日志）的身份；路径相对于证据库根目录。"""

    name: Identifier
    digest: Digest
    path: Annotated[str, StringConstraints(pattern=r"^[^/\x00][^\x00]{0,511}$")]


TrialStatus = Literal["completed", "system_error", "timeout", "infra_error", "missing"]


class TrialRecord(Contract):
    """计划中的一个槽位；缺失或基础设施失败也占位，保证分母完整。"""

    slot: int = Field(ge=0)
    arm_id: Identifier
    item_id: Identifier
    attempt: int = Field(ge=0)
    status: TrialStatus
    reason: Identifier
    scores: tuple[Score, ...] = Field(default=(), max_length=64)
    usage: Usage = Usage()
    cost: Cost = Cost()
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    artifacts: tuple[ArtifactRef, ...] = Field(default=(), max_length=64)
    executor_refs: dict[Identifier, ShortText] = Field(default_factory=dict, max_length=16)
    system_refs: dict[Identifier, ShortText] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def evidence_is_consistent(self) -> Self:
        if len({(s.grader_id, s.name) for s in self.scores}) != len(self.scores):
            raise ValueError("同一判分器的同名分数重复")
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError("试次时间倒置")
        if self.status == "missing" and (self.scores or self.artifacts):
            raise ValueError("缺失试次不能携带分数或制品")
        return self

    def score(self, grader_id: str, name: str) -> Score | None:
        return next((s for s in self.scores if s.grader_id == grader_id and s.name == name), None)


# ---------------------------------------------------------------- 运行报告

Outcome = Literal["passed", "failed", "invalid"]


def trial_outcome(trial: TrialRecord, primary: GraderSpec) -> Outcome:
    """主指标判定：基础设施失败或缺失为invalid；其余由主判分器的布尔通过分数决定。"""
    if trial.status in ("infra_error", "missing"):
        return "invalid"
    if trial.status in ("system_error", "timeout"):
        return "failed"
    score = trial.score(primary.grader_id, primary.pass_score)
    if score is None or not isinstance(score.value, bool):
        return "invalid"
    return "passed" if score.value else "failed"


class ArmSummary(Contract):
    arm_id: Identifier
    scheduled: int = Field(ge=1)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    invalid: int = Field(ge=0)


class EvalRunReport(Contract):
    """绑定计划摘要的完整报告；汇总可由全部试次重算。"""

    spec_version: Literal["evalkit.run-report/v1"] = "evalkit.run-report/v1"
    plan: RunPlan
    plan_digest: Digest
    trials: tuple[TrialRecord, ...] = Field(min_length=1, max_length=100_000)
    summary: tuple[ArmSummary, ...]
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def report_is_recomputable(self) -> Self:
        if self.plan_digest != self.plan.digest:
            raise ValueError("报告未绑定当前计划")
        expected = self.plan.slots()
        observed = tuple((t.arm_id, t.item_id, t.attempt) for t in self.trials)
        if observed != expected or tuple(t.slot for t in self.trials) != tuple(
            range(len(expected))
        ):
            raise ValueError("试次必须按计划顺序完整覆盖全部槽位")
        if self.summary != summarize(self.plan, self.trials):
            raise ValueError("汇总与全部试次不一致")
        if self.completed_at < self.plan.created_at:
            raise ValueError("运行结束早于计划冻结")
        return self


def summarize(plan: RunPlan, trials: tuple[TrialRecord, ...]) -> tuple[ArmSummary, ...]:
    primary = next(g for g in plan.graders if g.grader_id == plan.primary_grader)
    result = []
    for arm in plan.arms:
        outcomes = [trial_outcome(t, primary) for t in trials if t.arm_id == arm.arm_id]
        result.append(
            ArmSummary(
                arm_id=arm.arm_id,
                scheduled=len(outcomes),
                passed=outcomes.count("passed"),
                failed=outcomes.count("failed"),
                invalid=outcomes.count("invalid"),
            )
        )
    return tuple(result)
