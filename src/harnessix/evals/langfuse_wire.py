"""原生Suite和Harbor报告共用的OTLP/评分编码；不读取文件或发送网络请求。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import JsonValue


def identifier(*parts: str) -> str:
    """用域分隔的证据身份生成稳定标识，不包含操作者凭据。"""
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def attributes(values: dict[str, str | int | bool]) -> list[JsonValue]:
    """按OTLP JSON类型编码白名单属性，整数使用协议要求的十进制字符串。"""
    result: list[JsonValue] = []
    for key, value in values.items():
        encoded: dict[str, JsonValue]
        if isinstance(value, bool):
            encoded = {"boolValue": value}
        elif isinstance(value, int):
            encoded = {"intValue": str(value)}
        else:
            encoded = {"stringValue": value}
        result.append({"key": key, "value": encoded})
    return result


def _nanoseconds(value: datetime) -> str:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return str((delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000)


def trial_span(
    trace_id: str,
    span_id: str,
    started_at: datetime,
    completed_at: datetime,
    passed: bool,
    metadata: dict[str, str | int | bool],
) -> dict[str, JsonValue]:
    """编码一个真实证据窗口的摘要Span，不生成虚构的逐模型调用。"""
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "harnessix.eval.trial",
        "kind": 1,
        "startTimeUnixNano": _nanoseconds(started_at),
        "endTimeUnixNano": _nanoseconds(completed_at),
        "attributes": attributes(metadata),
        "status": {"code": 1 if passed else 2},
    }


def score_events(
    trace_id: str, span_id: str, values: dict[str, float | str], completed_at: datetime
) -> list[dict[str, JsonValue]]:
    """评分身份及时间固定于源证据，重发不会创建新的评测试次。"""
    return [
        {
            "id": identifier("harnessix.eval.score/v1", trace_id, name),
            "traceId": trace_id,
            "observationId": span_id,
            "name": name,
            "dataType": "CATEGORICAL" if isinstance(value, str) else "NUMERIC",
            "value": value,
            "timestamp": completed_at.isoformat(),
            "environment": "evaluation",
        }
        for name, value in values.items()
    ]
