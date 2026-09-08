from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from time import monotonic
from typing import Literal
from uuid import UUID

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import AgentFailure, FailureCategory, KernelError
from harnessix.agent.models import Turn, Usage
from harnessix.context.contracts import (
    ContextInspectionRecord,
    ContextInspectionV2,
    ContextInspectionV3,
)
from harnessix.context.tool_result_contracts import ModelHistoryInspectionRecord
from harnessix.domain.models import TraceContext
from harnessix.observability.core import Observability, ObservabilitySpan

_LOGGER = logging.getLogger(__name__)
OperationName = Literal[
    "turn",
    "model",
    "tool",
    "approval",
    "cancel",
    "recovery",
    "context",
    "history",
    "compaction",
]
ThreadLifecycleAction = Literal["resume", "fork", "archive"]
ThreadLifecycleOutcome = Literal["completed", "idempotent", "rejected"]
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

    def context(self, inspection: ContextInspectionRecord) -> None:
        values = {
            "available": inspection.available_input_tokens,
            "history": inspection.history_tokens,
            "tools": inspection.tool_tokens,
            "instructions": inspection.instruction_tokens,
            "estimated_input": inspection.estimated_input_tokens,
        }
        for component, value in values.items():
            self._send(
                partial(
                    self.observability.record,
                    "harnessix.agent.context.tokens",
                    value,
                    attributes={"component": component},
                )
            )
        for fragment in inspection.fragments:
            self._send(
                partial(
                    self.observability.increment,
                    "harnessix.agent.context.fragments",
                    attributes={
                        "kind": fragment.kind.value,
                        "disposition": fragment.disposition,
                    },
                )
            )
        if isinstance(inspection, ContextInspectionV2 | ContextInspectionV3):
            for source in inspection.sources:
                self._send(
                    partial(
                        self.observability.increment,
                        "harnessix.agent.context.sources",
                        attributes={"kind": source.kind.value, "status": source.status},
                    )
                )
        if isinstance(inspection, ContextInspectionV3):
            self._send(
                partial(
                    self.observability.increment,
                    "harnessix.agent.context.consistency",
                    attributes={
                        "strategy": inspection.consistency.strategy,
                        "result": "stable",
                    },
                )
            )

    def model_history(self, inspection: ModelHistoryInspectionRecord) -> None:
        values = {
            "source": inspection.source_tool_result_utf8_bytes,
            "view": inspection.view_tool_result_utf8_bytes,
        }
        for component, value in values.items():
            self._send(
                partial(
                    self.observability.record,
                    "harnessix.agent.model_history.tool_result_bytes",
                    value,
                    attributes={"component": component},
                )
            )
        for strategy, count in (
            ("inline", inspection.inline_results),
            ("artifact_reference", inspection.artifact_reference_results),
        ):
            self._send(
                partial(
                    self.observability.increment,
                    "harnessix.agent.model_history.tool_results",
                    count,
                    attributes={"strategy": strategy},
                )
            )
        self._send(
            partial(
                self.observability.record,
                "harnessix.agent.model_history.artifact_bindings",
                inspection.artifact_bindings,
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

    def thread_lifecycle(
        self,
        action: ThreadLifecycleAction,
        outcome: ThreadLifecycleOutcome,
        *,
        inherited_items: int = 0,
    ) -> None:
        labels = {"action": action, "outcome": outcome}
        self._send(
            lambda: self.observability.increment(
                "harnessix.agent.thread.lifecycle", attributes=labels
            )
        )
        if action == "fork":
            self._send(
                lambda: self.observability.record(
                    "harnessix.agent.thread.fork.inherited_items",
                    inherited_items,
                )
            )
