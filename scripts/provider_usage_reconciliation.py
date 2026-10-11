"""BETA-001 Max 请求的追加式 usage 对账合同。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field

from harnessix.agent.usage import TokenCount
from harnessix.domain.models import ContractModel
from harnessix.models.pricing import Amount, Digest, amount_units
from harnessix.tools.workspace import digest

BUDGET_SCHEMA = "harnessix.provider-verification-budget/v6"
RECONCILIATION_VERSION = "harnessix.provider-usage-reconciliation/v1"
MAX_RECONCILIATIONS = 10_000
MAX_MODEL = "qwen3-max-2025-09-23"
MAX_PRICE_BASIS = "https://help.aliyun.com/zh/model-studio/model-qwen3-max"
MAX_INPUT_TOKENS = 258_048
MAX_PRICE_TIERS = (
    (32_000, 6_000, 24_000),
    (128_000, 10_000, 40_000),
    (262_144, 15_000, 60_000),
)
MilliRate = Annotated[int, Field(ge=1, le=1_000_000, strict=True)]


class UsageReconciliation(ContractModel):
    """把完整认证 usage 结算为已知费用，不改写原 unknown 请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    spec_version: Literal["harnessix.provider-usage-reconciliation/v1"]
    authority: Literal["budget-owner-explicit"]
    reconciliation_id: UUID
    previous_reconciliation_id: UUID | None
    period_id: UUID
    request_id: UUID
    request_sha256: Digest
    ledger_before_sha256: Digest
    source_evidence_sha256: Digest
    requested_model: Literal["qwen3-max-2025-09-23"]
    actual_model: Literal["qwen3-max-2025-09-23"]
    price_basis: Literal["https://help.aliyun.com/zh/model-studio/model-qwen3-max"]
    input_tokens: TokenCount
    output_tokens: TokenCount
    input_rate_milli_per_million: MilliRate
    output_rate_milli_per_million: MilliRate
    cost_estimate: Amount
    failure_code: Literal["provider_invalid_provider_output"]
    diagnostic_suffix: Literal["chat_protocol/v1:tool_name_unknown"]
    recorded_at: AwareDatetime


def parse_usage_reconciliation(text: str) -> UsageReconciliation:
    """严格解析单条记录；调用方负责限制外围账本总字节数。"""
    return UsageReconciliation.model_validate_json(text, strict=True)


def snapshot_usage_reconciliation(value: object) -> UsageReconciliation:
    """重建不可信模型实例，拒绝 copy/construct 或可变内部状态绕过。"""
    if (
        type(value) is not UsageReconciliation
        or set(value.__dict__) != set(UsageReconciliation.model_fields)
        or value.__pydantic_extra__ is not None
    ):
        raise ValueError
    checked = UsageReconciliation.model_validate(dict(value.__dict__), strict=True)
    return parse_usage_reconciliation(checked.model_dump_json())


def usage_reconciliations(period: dict[str, Any]) -> tuple[UsageReconciliation, ...]:
    raw = period.get("usage_reconciliations", [])
    if type(raw) is not list or len(raw) > MAX_RECONCILIATIONS:
        raise ValueError
    records = tuple(parse_usage_reconciliation(json.dumps(item)) for item in raw)
    seen_records: set[UUID] = set()
    seen_requests: set[UUID] = set()
    previous: UUID | None = None
    for record in records:
        if (
            record.reconciliation_id in seen_records
            or record.request_id in seen_requests
            or record.previous_reconciliation_id != previous
        ):
            raise ValueError
        validate_usage_reconciliation(period, record)
        seen_records.add(record.reconciliation_id)
        seen_requests.add(record.request_id)
        previous = record.reconciliation_id
    return records


def reconciliation_costs(period: dict[str, Any]) -> dict[str, int]:
    return {
        str(record.request_id): amount_units(record.cost_estimate)
        for record in usage_reconciliations(period)
    }


def validate_usage_reconciliation(
    period: dict[str, Any], record: UsageReconciliation
) -> dict[str, Any]:
    """绑定原请求、固定北京快照价和完整用量证据。"""
    record = snapshot_usage_reconciliation(record)
    if str(record.period_id) != period.get("period_id"):
        raise ValueError
    matches = [
        request
        for request in period.get("requests", [])
        if request.get("request_id") == str(record.request_id)
    ]
    if len(matches) != 1:
        raise ValueError
    request = matches[0]
    if (
        request.get("status") != "unknown"
        or request.get("reserved_cost") is None
        or request.get("requested_model") != record.requested_model
        or request.get("price_basis") != record.price_basis
        or request.get("max_attempts") != 1
        or type(request.get("max_output_tokens")) is not int
        or record.output_tokens > request["max_output_tokens"]
        or record.input_tokens > MAX_INPUT_TOKENS
        or record.request_sha256 != digest(request)
    ):
        raise ValueError
    input_rate, output_rate = next(
        (input_rate, output_rate)
        for limit, input_rate, output_rate in MAX_PRICE_TIERS
        if record.input_tokens <= limit
    )
    cost = (record.input_tokens * input_rate + record.output_tokens * output_rate) * 10**9
    if (
        record.input_rate_milli_per_million != input_rate
        or record.output_rate_milli_per_million != output_rate
        or amount_units(record.cost_estimate) != cost
        or cost > amount_units(request["reserved_cost"])
        or record.recorded_at < datetime.fromisoformat(request["completed_at"])
    ):
        raise ValueError
    return request
