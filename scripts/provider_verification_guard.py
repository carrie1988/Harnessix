"""固定北京Coder快照的验证请求保护；不提供通用产品计价或账户硬限额。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.models.contracts import ModelProvider, ModelRequest, ProviderEvent, ResponseCompleted
from harnessix.tools.workspace import digest
from scripts.provider_verification_budget import VerificationBudgetLedger

MODEL = "qwen3-coder-plus-2025-09-23"
PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/qwen3-coder-plus"


@dataclass(frozen=True, slots=True)
class BailianVerificationBounds:
    """可信宿主核验的有限价格窗口；最高档预留，不削减模型输入以凑预算。"""

    valid_from: datetime
    valid_until: datetime
    max_output_tokens: int

    def checkpoint(self) -> None:
        now = datetime.now(UTC)
        if (
            self.valid_from.tzinfo is None
            or self.valid_until.tzinfo is None
            or not (self.valid_from <= now < self.valid_until)
            or type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= 4096
        ):
            raise KernelError("verification_price_unavailable", "验证价格窗口或输出边界不可用")

    @property
    def maximum_units(self) -> int:
        # 最大输入997952、最高输入20/输出200元每百万；不计缓存、免费额度或折扣。
        return (997_952 * 20 + self.max_output_tokens * 200) * 10**12

    def cost_units(self, usage: UsageObservation) -> int | None:
        if (
            usage.completeness != "complete"
            or usage.input_tokens is None
            or usage.output_tokens is None
            or usage.input_tokens > 997_952
            or usage.output_tokens > self.max_output_tokens
        ):
            return None
        input_rate, output_rate = next(
            (ir, outr)
            for limit, ir, outr in (
                (32_000, 4, 16),
                (128_000, 6, 24),
                (256_000, 10, 40),
                (1_000_000, 20, 200),
            )
            if usage.input_tokens <= limit
        )
        return (usage.input_tokens * input_rate + usage.output_tokens * output_rate) * 10**12

    def fingerprint(self, ledger: VerificationBudgetLedger) -> str:
        return digest(
            {
                "spec_version": "harnessix.bailian-verification-request-guard/v1",
                "model": MODEL,
                "region": "cn-beijing",
                "mode": "non-thinking",
                "price_source": PRICE_SOURCE,
                "valid_from": self.valid_from.isoformat(),
                "valid_until": self.valid_until.isoformat(),
                "max_output_tokens": self.max_output_tokens,
                "max_attempts": 1,
                "maximum_units": self.maximum_units,
                "period_id": ledger.period_id,
                "ledger_locator_sha256": digest(str(ledger.path)),
                "allocation_units": ledger.allocation,
            }
        )


@dataclass(slots=True)
class GuardedVerificationProvider:
    """托管官方Adapter的单请求预留与结算；歧义会取消整个Suite。"""

    provider: ModelProvider
    ledger: VerificationBudgetLedger
    bounds: BailianVerificationBounds
    suite_cancel: CancelToken

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        cancel.checkpoint()
        self.suite_cancel.checkpoint()
        try:
            self.bounds.checkpoint()
            reservation = self.ledger.reserve(
                self.bounds.maximum_units,
                {
                    "purpose": "engineering-suite-model-request",
                    "requested_model": MODEL,
                    "region": "cn-beijing",
                    "max_output_tokens": self.bounds.max_output_tokens,
                    "max_attempts": 1,
                    "thread_id": str(request.thread_id),
                    "turn_id": str(request.turn_id),
                    "step": request.step,
                    "price_basis": PRICE_SOURCE,
                },
            )
        except Exception:
            self.suite_cancel.cancel()
            raise
        attempt: UUID | None = None
        possibly_sent = False
        usage = UsageObservation()
        actual_model: str | None = None
        finished = False
        attempt_ended = False
        model_mismatch = False
        completion: ResponseCompleted | None = None
        clean_end = False
        try:
            async with aclosing(self.provider.stream(request, cancel)) as events:
                while True:
                    cancel.checkpoint()
                    try:
                        # Suite和Turn有不同令牌；复用run回收子任务，避免只取消后续Case。
                        event = await self.suite_cancel.run(anext(events))
                    except StopAsyncIteration:
                        clean_end = True
                        break
                    if completion is not None:
                        raise KernelError(
                            "verification_provider_protocol_invalid", "验证响应终态后仍有事件"
                        )
                    if isinstance(event, ModelAttemptStarted):
                        # 标记异常也不能证明未发送；失败时保留预留，不自动退款。
                        possibly_sent = True
                        if (
                            attempt is not None
                            or event.index != 1
                            or event.requested_model != MODEL
                            or event.provider != "openai_chat"
                            or event.step != request.step
                        ):
                            raise KernelError(
                                "verification_provider_protocol_invalid",
                                "验证请求尝试不符合固定边界",
                            )
                        attempt = event.attempt_id
                    elif isinstance(event, ModelUsageObserved):
                        if event.attempt_id != attempt or attempt_ended:
                            raise KernelError(
                                "verification_provider_protocol_invalid", "验证用量不属于当前尝试"
                            )
                        event.usage.validate_successor(usage)
                        usage = event.usage
                        if event.actual_model is not None:
                            model_mismatch |= event.actual_model != MODEL
                            actual_model = event.actual_model
                    elif isinstance(event, ModelAttemptFinished):
                        if event.attempt_id != attempt or attempt_ended:
                            raise KernelError(
                                "verification_provider_protocol_invalid", "验证尝试终态不一致"
                            )
                        finished = event.outcome == "completed"
                        attempt_ended = True
                    if isinstance(event, ResponseCompleted):
                        completion = event
                    else:
                        yield event
        finally:
            cost = (
                self.bounds.cost_units(usage)
                if (
                    clean_end
                    and completion is not None
                    and finished
                    and actual_model == MODEL
                    and not model_mismatch
                    and completion.usage.input_tokens == usage.input_tokens
                    and completion.usage.output_tokens == usage.output_tokens
                )
                else None
            )
            if possibly_sent and cost is None:
                self.suite_cancel.cancel()
            try:
                self.ledger.settle(reservation, cost, sent=possibly_sent)
            except Exception:
                self.suite_cancel.cancel()
                raise
        # 只有持久结算成功后才向Agent发布成功终态；未知费用不继续下一请求。
        if completion is not None:
            if cost is None:
                raise KernelError("verification_request_cost_unknown", "验证请求费用不能可靠确认")
            yield completion
