from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic
from typing import Literal
from uuid import UUID

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import AgentFailure, FailureCategory, KernelError
from harnessix.agent.models import Turn, Usage
from harnessix.domain.models import TraceContext
from harnessix.observability.core import Observability, ObservabilitySpan

_LOGGER = logging.getLogger(__name__)
OperationName = Literal["turn", "model", "tool", "approval", "cancel", "recovery"]
_OUTCOMES = frozenset(
    {
        "ok",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "waiting_approval",
        "approved",
        "rejected",
        "succeeded",
        "unknown",
        "cancelling",
    }
)


class Operation:
    def __init__(self, bind_turn: Callable[[UUID], None]) -> None:
        self.bind_turn = bind_turn
        self.outcome = "ok"
        self.category: FailureCategory | None = None

    def finish(self, outcome: str, failure: AgentFailure | None = None) -> None:
        self.outcome = outcome if outcome in _OUTCOMES else "failed"
        self.category = (
            failure.category
            if failure
            else (
                FailureCategory.INTERRUPTED
                if outcome == "unknown"
                else FailureCategory.INTERNAL
                if outcome == "failed"
                else None
            )
        )


class KernelTelemetry:
    """有限标签、无业务异常泄漏、导出失败不干扰执行。"""

    def __init__(self, observability: Observability) -> None:
        self.observability = observability
        self._broken = False

    def _degrade(self) -> None:
        if not self._broken:
            self._broken = True
            try:
                _LOGGER.warning("Agent 可观测性输出失败，当前宿主已降级为无导出")
            except Exception:
                pass  # 日志 Handler 本身失败也不能改变业务结果。

    def _send(self, operation: Callable[[], None]) -> None:
        if not self._broken:
            try:
                operation()
            except Exception:
                self._degrade()

    def trace_context(self) -> TraceContext | None:
        if not self._broken:
            try:
                context = self.observability.current_trace_context()
                return (
                    TraceContext.model_validate_json(context.model_dump_json()) if context else None
                )
            except Exception:
                self._degrade()
        return None

    @contextmanager
    def operation(
        self,
        name: OperationName,
        *,
        thread_id: UUID,
        turn_id: UUID,
        trace_context: TraceContext | None = None,
        call_id: UUID | None = None,
        step: int | None = None,
    ) -> Iterator[Operation]:
        attributes: dict[str, str | int] = {
            "thread_id": str(thread_id),
            "turn_id": str(turn_id),
        }
        if call_id is not None:
            attributes["call_id"] = str(call_id)
        if step is not None:
            attributes["model_step"] = step
        scope = None
        span: ObservabilitySpan | None = None
        if not self._broken:
            try:
                candidate = self.observability.span(
                    f"harnessix.agent.{name}",
                    trace_context=trace_context,
                    attributes=attributes,
                )
                span = candidate.__enter__()
                scope = candidate
            except Exception:
                self._degrade()

        def bind_turn(turn: UUID) -> None:
            if span is not None:
                self._send(lambda: span.set_attribute("turn_id", str(turn)))

        result = Operation(bind_turn)
        started = monotonic()
        try:
            yield result
        except BaseException as error:
            if isinstance(error, TurnCancelled | asyncio.CancelledError):
                result.finish("cancelled", AgentFailure(code="cancelled", message="操作被取消"))
            elif isinstance(error, KernelError):
                result.finish("failed", error.to_failure())
            elif isinstance(error, Exception):
                result.finish("failed", AgentFailure(code="runtime_error", message="操作失败"))
            else:
                result.finish(
                    "interrupted", AgentFailure(code="process_interrupted", message="操作中断")
                )
            raise
        finally:
            labels = {"operation": name, "outcome": result.outcome}
            if result.category is not None:
                labels["category"] = result.category.value
            if span is not None:
                self._send(lambda: span.set_attribute("outcome", result.outcome))
                if result.category is not None and result.outcome not in {
                    "cancelled",
                    "cancelling",
                }:
                    category = result.category.value
                    self._send(lambda: span.set_error(category))
            if scope is not None:
                try:
                    # 不把任何业务异常交给第三方自动 exception/stacktrace 采集逻辑。
                    scope.__exit__(None, None, None)
                except Exception:
                    self._degrade()
            self._send(
                lambda: self.observability.increment(
                    "harnessix.agent.operations", attributes=labels
                )
            )
            self._send(
                lambda: self.observability.record(
                    "harnessix.agent.operation.duration",
                    monotonic() - started,
                    attributes=labels,
                )
            )

    def usage(self, usage: Usage) -> None:
        self._send(
            lambda: self.observability.increment("harnessix.agent.tokens.input", usage.input_tokens)
        )
        self._send(
            lambda: self.observability.increment(
                "harnessix.agent.tokens.output", usage.output_tokens
            )
        )

    def finished(self, turn: Turn) -> None:
        labels = {"status": turn.status.value}
        if turn.error is not None:
            labels["category"] = turn.error.category.value
        self._send(
            lambda: self.observability.increment(
                "harnessix.agent.turns.finished", attributes=labels
            )
        )
