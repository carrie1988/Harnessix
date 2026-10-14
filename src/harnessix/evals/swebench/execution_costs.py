"""SWE-bench费用准入和可隔离传输失败；只解释原账本，不启动请求。"""

from __future__ import annotations

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Turn, TurnStatus
from harnessix.agent.usage import ModelAttempt
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.run_config import SweBenchRunConfig as SweBenchAgentRunConfig
from harnessix.models.costs import bind_price, build_cost_report, estimate_attempt
from harnessix.models.pricing import FlatInputPrice, amount_units, rate_units


def _closed_single_attempt_history(turn: Turn) -> bool:
    """每步恰有一次已闭合请求，且摘要没有未知外部尝试。"""
    return bool(
        turn.accounted_attempts
        and turn.model_attempts
        and all(
            attempt.status in {"completed", "failed"} and attempt.index == 1
            for attempt in turn.accounted_attempts
        )
        and not any(record.unaccounted_request_possible for record in turn.compactions)
        and len(turn.model_attempts) == turn.model_steps
        and {attempt.step for attempt in turn.model_attempts} == set(range(1, turn.model_steps + 1))
    )


def _isolatable_attempt(attempt: ModelAttempt, config: SweBenchAgentRunConfig) -> bool:
    """固定模型与有效价格窗口不能因费用预留而被绕过。"""
    return (
        attempt.requested_model == config.provider_config.model
        and attempt.provider == "openai_chat"
        and attempt.actual_model in {None, attempt.requested_model}
        and config.price.valid_from <= attempt.started_at < config.price.valid_until
        and attempt.finished_at is not None
        and attempt.finished_at < config.price.valid_until
        and (
            attempt.usage.completeness != "complete"
            or (attempt.actual_model == config.price.model and attempt.response_id is not None)
        )
    )


def _can_isolate_transport_failure(turn: Turn, config: SweBenchAgentRunConfig) -> bool:
    """费用可预留，但未闭合尝试、未知摘要和缺失原生请求证明不能据此继续。"""
    attempts = turn.accounted_attempts
    if (
        turn.status is not TurnStatus.FAILED
        or turn.error is None
        or turn.error.code != "provider_transport"
        or not _closed_single_attempt_history(turn)
    ):
        return False
    last = turn.model_attempts[-1]
    if last.status != "failed" or last.error is None or last.error.code != "provider_transport":
        return False
    if not all(_isolatable_attempt(attempt, config) for attempt in attempts):
        return False
    for attempt in attempts:
        result = estimate_attempt(
            attempt, bind_price(attempt, config.price, config.billing_context)
        ).result
        if result.status != "estimated" and not (
            attempt.attempt_id == last.attempt_id and result.reason == "usage_incomplete"
        ):
            return False
    return True


def _turn_cost(turn: Turn, config: SweBenchAgentRunConfig) -> tuple[int, bool]:
    if not turn.accounted_attempts:
        return 0, False
    bindings = tuple(
        bind_price(attempt, config.price, config.billing_context)
        for attempt in turn.accounted_attempts
    )
    report = build_cost_report(turn, bindings)
    totals = report.summary.totals
    if len(totals) > 1 or (totals and totals[0].currency != config.price.currency):
        raise KernelError("swebench_cost_invalid", "SWE-bench模型费用币种无效")
    units = amount_units(totals[0].known_amount) if totals else 0
    return units, report.summary.completeness == "complete"


def _input_rate(config: SweBenchAgentRunConfig) -> int:
    price = config.price.input_price
    if not isinstance(price, FlatInputPrice):
        raise KernelError("swebench_cost_invalid", "SWE-bench输入价格合同无效")
    return rate_units(price.per_million)


def _unknown_request_reserve(config: SweBenchAgentRunConfig) -> int:
    return config.provider_config.max_request_bytes * _input_rate(config) + (
        config.provider_config.max_output_tokens + config.output_token_limit_margin
    ) * rate_units(config.price.output_per_million)


def _next_case_reserve(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchAgentRunConfig,
) -> int:
    known = (plan.task_budget.max_tokens + config.output_token_limit_margin) * max(
        _input_rate(config),
        rate_units(config.price.output_per_million),
    )
    return known + _unknown_request_reserve(config)
