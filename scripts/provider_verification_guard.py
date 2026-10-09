"""北京Coder验证保护；固定评测快照与独立Beta配置，不提供通用账户硬限额。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
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


class VerificationRequestPacer:
    """同一验证 Suite 的发送间隔；不是账户级限流，也不重试或延长 Turn。"""

    def __init__(self, minimum_interval_seconds: float) -> None:
        if (
            type(minimum_interval_seconds) not in {int, float}
            or not math.isfinite(minimum_interval_seconds)
            or not 0 <= minimum_interval_seconds <= 60
        ):
            raise KernelError("verification_request_pacing_invalid", "验证发送间隔无效")
        self._minimum_interval_seconds = float(minimum_interval_seconds)
        self._next_start = 0.0
        self._lock = asyncio.Lock()

    async def wait(self, cancel: CancelToken, suite_cancel: CancelToken) -> None:
        cancel.checkpoint()
        suite_cancel.checkpoint()
        # 托管整个临界段，而非单独 acquire，避免取消竞态遗留已获取的锁。
        await cancel.run(suite_cancel.run(self._wait()))
        cancel.checkpoint()
        suite_cancel.checkpoint()

    @property
    def minimum_interval_seconds(self) -> float:
        return self._minimum_interval_seconds

    async def _wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next_start - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_start = loop.time() + self.minimum_interval_seconds


@dataclass(frozen=True, slots=True)
class BailianVerificationBounds:
    """可信宿主核验的有限价格窗口；最高档预留，不削减模型输入以凑预算。"""

    model: ClassVar[str] = MODEL
    price_source: ClassVar[str] = PRICE_SOURCE
    purpose: ClassVar[str] = "engineering-suite-model-request"
    guard_version: ClassVar[str] = "harnessix.bailian-verification-request-guard/v1"
    maximum_input_tokens: ClassVar[int] = 997_952
    # 单位为毫元/百万Token，避免1.5等阶梯单价经过浮点运算。
    price_tiers: ClassVar[tuple[tuple[int, int, int], ...]] = (
        (32_000, 4_000, 16_000),
        (128_000, 6_000, 24_000),
        (256_000, 10_000, 40_000),
        (1_000_000, 20_000, 200_000),
    )

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
        # 完整输入上限、最高价格档预留；不把Token估算当硬上限，不扣除优惠。
        _, input_rate, output_rate = self.price_tiers[-1]
        return (
            self.maximum_input_tokens * input_rate + self.max_output_tokens * output_rate
        ) * 10**9

    def cost_units(self, usage: UsageObservation) -> int | None:
        if (
            usage.completeness != "complete"
            or usage.input_tokens is None
            or usage.output_tokens is None
            or usage.input_tokens > self.maximum_input_tokens
            or usage.output_tokens > self.max_output_tokens
        ):
            return None
        input_rate, output_rate = next(
            (ir, outr) for limit, ir, outr in self.price_tiers if usage.input_tokens <= limit
        )
        return (usage.input_tokens * input_rate + usage.output_tokens * output_rate) * 10**9

    def fingerprint(
        self, ledger: VerificationBudgetLedger, *, pacer: VerificationRequestPacer | None = None
    ) -> str:
        binding: dict[str, object] = {
            "spec_version": self.guard_version,
            "model": self.model,
            "region": "cn-beijing",
            "mode": "non-thinking",
            "price_source": self.price_source,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "max_output_tokens": self.max_output_tokens,
            "max_attempts": 1,
            "maximum_units": self.maximum_units,
            "period_id": ledger.period_id,
            "ledger_locator_sha256": digest(str(ledger.path)),
            "allocation_units": ledger.allocation,
        }
        if ledger.reverification_plan is not None:
            binding["bounded_reverification"] = ledger.reverification_plan.model_dump(mode="json")
        if ledger.active_reverification_binding is not None:
            binding["reverification_binding"] = ledger.active_reverification_binding.model_dump(
                mode="json"
            )
        if "reverification_binding_chain" in ledger.period:
            binding["reverification_binding_chain"] = ledger.period["reverification_binding_chain"]
        if pacer is not None and pacer.minimum_interval_seconds > 0:
            binding["request_pacing"] = {
                "spec_version": "harnessix.verification-request-pacing/v1",
                "minimum_interval_seconds": pacer.minimum_interval_seconds,
                "scope": "single-suite",
                "automatic_retry": False,
            }
        return digest(binding)


@dataclass(frozen=True, slots=True)
class BailianBetaVerificationBounds(BailianVerificationBounds):
    """仅BETA-001的Coder Next北京价；不改变R3固定模型或评测计分。"""

    model: ClassVar[str] = "qwen3-coder-next"
    price_source: ClassVar[str] = "https://help.aliyun.com/zh/model-studio/qwen3-coder-next"
    purpose: ClassVar[str] = "beta-001-model-request"
    guard_version: ClassVar[str] = "harnessix.bailian-beta-request-guard/v1"
    maximum_input_tokens: ClassVar[int] = 204_800
    price_tiers: ClassVar[tuple[tuple[int, int, int], ...]] = (
        (32_000, 1_000, 4_000),
        (128_000, 1_500, 6_000),
        (262_144, 2_500, 10_000),
    )


@dataclass(slots=True)
class GuardedVerificationProvider:
    """托管官方Adapter的单请求预留与结算；歧义会取消整个Suite。"""

    provider: ModelProvider
    ledger: VerificationBudgetLedger
    bounds: BailianVerificationBounds
    suite_cancel: CancelToken
    pacer: VerificationRequestPacer | None = None

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        cancel.checkpoint()
        self.suite_cancel.checkpoint()
        if self.pacer is not None:
            # 等待发生在持久费用预留之前；取消不制造“已发送”或费用未决事实。
            await self.pacer.wait(cancel, self.suite_cancel)
        try:
            self.bounds.checkpoint()
            if isinstance(self.bounds, BailianBetaVerificationBounds) and (
                self.ledger.task_id != "BETA-001" or self.ledger.suite_id is not None
            ):
                raise KernelError("verification_budget_unresolved", "Beta模型只属于单任务授权")
            reservation = self.ledger.reserve(
                self.bounds.maximum_units,
                {
                    "purpose": self.bounds.purpose,
                    "requested_model": self.bounds.model,
                    "region": "cn-beijing",
                    "max_output_tokens": self.bounds.max_output_tokens,
                    "max_attempts": 1,
                    "thread_id": str(request.thread_id),
                    "turn_id": str(request.turn_id),
                    "step": request.step,
                    "price_basis": self.bounds.price_source,
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
                            or event.requested_model != self.bounds.model
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
                            model_mismatch |= event.actual_model != self.bounds.model
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
                    and actual_model == self.bounds.model
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
