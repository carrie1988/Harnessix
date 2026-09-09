from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from harnessix.agent.models import TERMINAL_TURNS, Turn, TurnStatus, TurnStatusV18
from harnessix.agent.usage import ModelAttempt, ModelIdentifier, TokenCount, UsageObservation
from harnessix.context.compaction_ledger_contracts import COMPACTION_OPEN
from harnessix.domain.models import ContractModel
from harnessix.models.billing import resolve_billing_context
from harnessix.models.pricing import (
    Amount,
    BillingContext,
    Currency,
    Digest,
    FlatInputPrice,
    PriceSnapshot,
    Rate,
    amount_units,
    content_digest,
    format_amount,
    rate_units,
)

CostCategory = Literal[
    "input", "uncached_input", "cache_read_input", "cache_creation_input", "output"
]
_SCOPE_FIELDS = ("billing_provider", "region", "service_tier", "inference_mode")


class CostAttempt(ContractModel):
    """只保留重算所需尝试事实；不复制错误原文、Prompt 或 HTTP 信息。"""

    attempt_id: UUID
    step: int = Field(ge=1, le=1000, strict=True)
    index: int = Field(ge=1, le=32, strict=True)
    actual_model: ModelIdentifier | None
    usage: UsageObservation
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        if (self.status == "running") != (self.finished_at is None):
            raise ValueError("尝试结束时间与状态不一致")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("尝试结束时间早于开始时间")
        return self

    @classmethod
    def from_attempt(cls, attempt: ModelAttempt) -> Self:
        return cls.model_validate(attempt.model_dump(include=set(cls.model_fields)))


class PriceBinding(ContractModel):
    attempt_id: UUID
    attempt_sha256: Digest
    price_sha256: Digest
    price: PriceSnapshot
    context: BillingContext

    @model_validator(mode="after")
    def validate_price(self) -> Self:
        if self.price_sha256 != self.price.digest:
            raise ValueError("价格内容与绑定哈希不一致")
        return self


def bind_price(
    attempt: ModelAttempt, price: PriceSnapshot, context: BillingContext
) -> PriceBinding:
    price = PriceSnapshot.model_validate_json(price.model_dump_json())
    context = resolve_billing_context(attempt, context)
    return PriceBinding(
        attempt_id=attempt.attempt_id,
        attempt_sha256=content_digest(CostAttempt.from_attempt(attempt)),
        price_sha256=price.digest,
        price=price,
        context=context,
    )


class CostLine(ContractModel):
    category: CostCategory
    tokens: TokenCount
    per_million: Rate
    amount: Amount


class CostResult(ContractModel):
    status: Literal["estimated", "unknown"]
    reason: (
        Literal[
            "price_unbound",
            "attempt_running",
            "usage_incomplete",
            "model_unknown",
            "context_missing",
            "scope_mismatch",
            "outside_price_period",
            "outside_input_band",
            "usage_details_missing",
            "cache_ttl_missing",
            "cache_ttl_mismatch",
        ]
        | None
    ) = None
    currency: Currency | None = None
    amount: Amount | None = None
    lines: tuple[CostLine, ...] = ()


def _calculate(attempt: CostAttempt, binding: PriceBinding | None) -> CostResult:
    if binding is None:
        return CostResult(status="unknown", reason="price_unbound")
    if binding.attempt_id != attempt.attempt_id or binding.attempt_sha256 != content_digest(
        attempt
    ):
        raise ValueError("价格绑定不属于当前尝试快照")
    price, context, usage = binding.price, binding.context, attempt.usage
    if attempt.status == "running":
        return CostResult(status="unknown", reason="attempt_running")
    if usage.completeness != "complete":
        return CostResult(status="unknown", reason="usage_incomplete")
    if attempt.actual_model is None:
        return CostResult(status="unknown", reason="model_unknown")
    if any(getattr(context, field) is None for field in _SCOPE_FIELDS):
        return CostResult(status="unknown", reason="context_missing")
    if attempt.actual_model != price.model or any(
        getattr(context, field) != getattr(price, field) for field in _SCOPE_FIELDS
    ):
        return CostResult(status="unknown", reason="scope_mismatch")
    if (
        attempt.started_at < price.valid_from
        or attempt.finished_at is None
        or attempt.finished_at >= price.valid_until
    ):
        return CostResult(status="unknown", reason="outside_price_period")
    assert usage.input_tokens is not None and usage.output_tokens is not None
    if usage.input_tokens < price.input_tokens_min or (
        price.input_tokens_max is not None and usage.input_tokens > price.input_tokens_max
    ):
        return CostResult(status="unknown", reason="outside_input_band")
    lines: list[CostLine] = []

    def add(category: CostCategory, tokens: int, rate: str) -> None:
        lines.append(
            CostLine(
                category=category,
                tokens=tokens,
                per_million=rate,
                amount=format_amount(tokens * rate_units(rate)),
            )
        )

    inputs = price.input_price
    if isinstance(inputs, FlatInputPrice):
        add("input", usage.input_tokens, inputs.per_million)
    else:
        if (
            usage.uncached_input_tokens is None
            or usage.cache_read_input_tokens is None
            or usage.cache_creation_input_tokens is None
        ):
            return CostResult(status="unknown", reason="usage_details_missing")
        if usage.cache_creation_input_tokens > 0 and inputs.cache_write_ttl is not None:
            if context.cache_write_ttl is None:
                return CostResult(status="unknown", reason="cache_ttl_missing")
            if context.cache_write_ttl != inputs.cache_write_ttl:
                return CostResult(status="unknown", reason="cache_ttl_mismatch")
        add("uncached_input", usage.uncached_input_tokens, inputs.uncached_per_million)
        add("cache_read_input", usage.cache_read_input_tokens, inputs.cache_read_per_million)
        add(
            "cache_creation_input",
            usage.cache_creation_input_tokens,
            inputs.cache_creation_per_million,
        )
    add("output", usage.output_tokens, price.output_per_million)
    return CostResult(
        status="estimated",
        currency=price.currency,
        lines=tuple(lines),
        amount=format_amount(sum(amount_units(line.amount) for line in lines)),
    )


class AttemptCost(ContractModel):
    attempt: CostAttempt
    binding: PriceBinding | None
    result: CostResult

    @model_validator(mode="after")
    def validate_calculation(self) -> Self:
        if self.result != _calculate(self.attempt, self.binding):
            raise ValueError("成本结果与价格/用量重算不一致")
        return self


def estimate_attempt(attempt: ModelAttempt, binding: PriceBinding | None = None) -> AttemptCost:
    source = CostAttempt.from_attempt(attempt)
    if binding is not None:
        binding = PriceBinding.model_validate_json(binding.model_dump_json())
        if resolve_billing_context(attempt, binding.context) != binding.context:
            raise ValueError("价格绑定未纳入已观测的计费上下文")
    return AttemptCost(attempt=source, binding=binding, result=_calculate(source, binding))


class CurrencySubtotal(ContractModel):
    currency: Currency
    known_amount: Amount


class CostSummary(ContractModel):
    completeness: Literal["complete", "partial", "unknown"]
    uncovered_steps: tuple[int, ...]
    totals: tuple[CurrencySubtotal, ...]


def _summarize(
    entries: tuple[AttemptCost, ...], model_steps: int, status: TurnStatus | TurnStatusV18
) -> CostSummary:
    ids: set[UUID] = set()
    pairs: set[tuple[int, int]] = set()
    by_step: dict[int, list[CostAttempt]] = {}
    totals: dict[Currency, int] = {}
    for entry in entries:
        attempt, result = entry.attempt, entry.result
        pair = (attempt.step, attempt.index)
        if attempt.attempt_id in ids or pair in pairs or attempt.step > model_steps:
            raise ValueError("重复尝试或步骤不属于报告")
        ids.add(attempt.attempt_id)
        pairs.add(pair)
        by_step.setdefault(attempt.step, []).append(attempt)
        if result.status == "estimated":
            assert result.currency is not None and result.amount is not None
            totals[result.currency] = totals.get(result.currency, 0) + amount_units(result.amount)
    for attempts in by_step.values():
        attempts.sort(key=lambda a: a.index)
        if [a.index for a in attempts] != list(range(1, len(attempts) + 1)) or any(
            a.status != "failed" for a in attempts[:-1]
        ):
            raise ValueError("尝试索引不连续或成功后发生重试")
    uncovered = tuple(sorted(set(range(1, model_steps + 1)) - by_step.keys()))
    complete = (
        status in TERMINAL_TURNS
        and bool(entries)
        and not uncovered
        and all(e.result.status == "estimated" for e in entries)
    )
    return CostSummary(
        completeness="complete" if complete else "partial" if totals else "unknown",
        uncovered_steps=uncovered,
        totals=tuple(
            CurrencySubtotal(currency=c, known_amount=format_amount(totals[c]))
            for c in sorted(totals)
        ),
    )


class CostReport(ContractModel):
    spec_version: Literal["harnessix.cost-report/v1"] = "harnessix.cost-report/v1"
    turn_id: UUID
    turn_status: TurnStatusV18
    model_steps: int = Field(ge=0, le=1000, strict=True)
    entries: tuple[AttemptCost, ...] = Field(max_length=32000)
    summary: CostSummary

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.summary != _summarize(self.entries, self.model_steps, self.turn_status):
            raise ValueError("成本报告汇总与尝试不一致")
        return self


class AccountedAttemptCost(AttemptCost):
    purpose: Literal["generation", "compaction"]
    compaction_id: UUID | None = None

    @model_validator(mode="after")
    def bound_purpose(self) -> Self:
        if (self.purpose == "compaction") != (self.compaction_id is not None):
            raise ValueError("摘要费用必须绑定独立压缩身份")
        return self


class CompactionCostState(ContractModel):
    compaction_id: UUID
    model_step: int = Field(ge=1, le=1000, strict=True)
    status: Literal["planned", "sampling", "summarized", "failed", "cancelled", "interrupted"]
    attempt_id: UUID | None
    unaccounted_request_possible: bool = False


def _summarize_accounted(
    entries: tuple[AccountedAttemptCost, ...],
    compactions: tuple[CompactionCostState, ...],
    model_steps: int,
    status: TurnStatus | TurnStatusV18,
) -> CostSummary:
    generation = tuple(e for e in entries if e.purpose == "generation")
    ordinary = _summarize(generation, model_steps, status)
    by_id = {c.compaction_id: c for c in compactions}
    if len(by_id) != len(compactions) or len({c.model_step for c in compactions}) != len(
        compactions
    ):
        raise ValueError("压缩身份或目标步骤重复")
    if len({e.attempt.attempt_id for e in entries}) != len(entries):
        raise ValueError("跨用途请求身份重复")
    summary_entries = {e.compaction_id: e for e in entries if e.purpose == "compaction"}
    if (
        len(summary_entries) != len(entries) - len(generation)
        or not set(summary_entries) <= by_id.keys()
    ):
        raise ValueError("摘要成本重复或不属于压缩记录")
    for compaction in compactions:
        if compaction.model_step > model_steps + 1:
            raise ValueError("摘要目标超过当前或下一普通步骤")
        entry = summary_entries.get(compaction.compaction_id)
        if compaction.attempt_id is None:
            if entry is not None or compaction.status in {"sampling", "summarized"}:
                raise ValueError("摘要阶段与请求事实不一致")
        elif (
            entry is None
            or entry.attempt.attempt_id != compaction.attempt_id
            or entry.attempt.step != compaction.model_step
            or entry.attempt.index != 1
            or compaction.status == "planned"
            or (compaction.status not in COMPACTION_OPEN and entry.attempt.status == "running")
            or (compaction.status == "summarized" and entry.attempt.status != "completed")
        ):
            raise ValueError("摘要费用缺失、归属或结算状态不一致")
    totals: dict[Currency, int] = {}
    for entry in entries:
        result = entry.result
        if result.status == "estimated":
            assert result.currency is not None and result.amount is not None
            totals[result.currency] = totals.get(result.currency, 0) + amount_units(result.amount)
    complete = (
        status in TERMINAL_TURNS
        and bool(entries)
        and not ordinary.uncovered_steps
        and all(e.result.status == "estimated" for e in entries)
        and all(c.status not in COMPACTION_OPEN for c in compactions)
        and not any(c.unaccounted_request_possible for c in compactions)
    )
    return CostSummary(
        completeness="complete" if complete else "partial" if totals else "unknown",
        uncovered_steps=ordinary.uncovered_steps,
        totals=tuple(
            CurrencySubtotal(currency=c, known_amount=format_amount(totals[c]))
            for c in sorted(totals)
        ),
    )


class CostReportV2(ContractModel):
    spec_version: Literal["harnessix.cost-report/v2"] = "harnessix.cost-report/v2"
    turn_id: UUID
    turn_status: TurnStatusV18
    model_steps: int = Field(ge=0, le=1000, strict=True)
    compactions: tuple[CompactionCostState, ...] = Field(min_length=1, max_length=1000)
    entries: tuple[AccountedAttemptCost, ...] = Field(max_length=33000)
    summary: CostSummary

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        expected = _summarize_accounted(
            self.entries, self.compactions, self.model_steps, self.turn_status
        )
        if self.summary != expected:
            raise ValueError("成本汇总与全用途账本不一致")
        return self


class CostReportV3(ContractModel):
    """支持0.8.3交互状态，并统一普通生成与压缩尝试。"""

    spec_version: Literal["harnessix.cost-report/v3"] = "harnessix.cost-report/v3"
    turn_id: UUID
    turn_status: TurnStatus
    model_steps: int = Field(ge=0, le=1000, strict=True)
    compactions: tuple[CompactionCostState, ...] = Field(default_factory=tuple, max_length=1000)
    entries: tuple[AccountedAttemptCost, ...] = Field(max_length=33000)
    summary: CostSummary

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        expected = _summarize_accounted(
            self.entries, self.compactions, self.model_steps, self.turn_status
        )
        if self.summary != expected:
            raise ValueError("成本汇总与全用途账本不一致")
        return self


CostReportRecord = Annotated[
    CostReport | CostReportV2 | CostReportV3, Field(discriminator="spec_version")
]
COST_REPORT_ADAPTER: TypeAdapter[CostReportRecord] = TypeAdapter(CostReportRecord)


def build_cost_report(turn: Turn, bindings: tuple[PriceBinding, ...] = ()) -> CostReportRecord:
    by_id = {binding.attempt_id: binding for binding in bindings}
    if len(by_id) != len(bindings) or not set(by_id).issubset(
        {a.attempt_id for a in turn.accounted_attempts}
    ):
        raise ValueError("存在重复或不属于本 Turn 的价格绑定")
    entries = tuple(estimate_attempt(a, by_id.get(a.attempt_id)) for a in turn.model_attempts)
    if turn.status == TurnStatus.WAITING_INPUT:
        accounted = tuple(
            AccountedAttemptCost(**entry.model_dump(), purpose="generation") for entry in entries
        ) + tuple(
            AccountedAttemptCost(
                **estimate_attempt(c.attempt, by_id.get(c.attempt.attempt_id)).model_dump(),
                purpose="compaction",
                compaction_id=c.plan.compaction_id,
            )
            for c in turn.compactions
            if c.attempt is not None
        )
        states = tuple(
            CompactionCostState(
                compaction_id=c.plan.compaction_id,
                model_step=c.plan.model_step,
                status=c.status,
                attempt_id=c.attempt.attempt_id if c.attempt is not None else None,
                unaccounted_request_possible=c.unaccounted_request_possible,
            )
            for c in turn.compactions
        )
        return CostReportV3(
            turn_id=turn.turn_id,
            turn_status=turn.status,
            model_steps=turn.model_steps,
            compactions=states,
            entries=accounted,
            summary=_summarize_accounted(accounted, states, turn.model_steps, turn.status),
        )
    if turn.compactions:
        accounted = tuple(
            AccountedAttemptCost(**e.model_dump(), purpose="generation") for e in entries
        ) + tuple(
            AccountedAttemptCost(
                **estimate_attempt(c.attempt, by_id.get(c.attempt.attempt_id)).model_dump(),
                purpose="compaction",
                compaction_id=c.plan.compaction_id,
            )
            for c in turn.compactions
            if c.attempt is not None
        )
        states = tuple(
            CompactionCostState(
                compaction_id=c.plan.compaction_id,
                model_step=c.plan.model_step,
                status=c.status,
                attempt_id=c.attempt.attempt_id if c.attempt is not None else None,
                unaccounted_request_possible=c.unaccounted_request_possible,
            )
            for c in turn.compactions
        )
        return CostReportV2(
            turn_id=turn.turn_id,
            turn_status=TurnStatusV18(turn.status),
            model_steps=turn.model_steps,
            compactions=states,
            entries=accounted,
            summary=_summarize_accounted(accounted, states, turn.model_steps, turn.status),
        )
    return CostReport(
        turn_id=turn.turn_id,
        turn_status=TurnStatusV18(turn.status),
        model_steps=turn.model_steps,
        entries=entries,
        summary=_summarize(entries, turn.model_steps, turn.status),
    )
