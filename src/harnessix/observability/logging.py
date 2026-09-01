from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_LOG_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("harnessix_log_context", default=None)
_ALLOWED_CONTEXT_KEYS = frozenset(
    {"action_id", "tenant_id", "tool", "worker_id", "trace_id", "span_id"}
)


@contextmanager
def bind_log_context(**values: object) -> Iterator[None]:
    current = (_LOG_CONTEXT.get() or {}).copy()
    current.update(
        {
            key: str(value)
            for key, value in values.items()
            if key in _ALLOWED_CONTEXT_KEYS and value is not None
        }
    )
    token = _LOG_CONTEXT.set(current)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def trace_log_fields(trace_context: str | None) -> dict[str, str]:
    if trace_context is None:
        return {}
    parts = trace_context.split("-")
    if len(parts) != 4:
        return {}
    return {"trace_id": parts[1], "span_id": parts[2]}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_LOG_CONTEXT.get() or {})
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(*, level: str = "INFO", log_format: str = "json") -> None:
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"不支持的日志级别：{level}")
    if log_format not in {"json", "console"}:
        raise ValueError("log_format 只能是 json 或 console")

    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)
