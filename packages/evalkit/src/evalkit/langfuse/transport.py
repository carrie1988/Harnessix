"""显式发送已生成的投影；不跟随重定向、不自动重试、不回传响应正文。"""

from __future__ import annotations

import json
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from evalkit.errors import EvalkitError
from evalkit.langfuse.contracts import LangfuseExport

_MAX_RESPONSE_BYTES = 65_536
_SCORE_BATCH_SIZE = 50
# 一次OTLP请求的正文上限：Langfuse worker整体解析一次请求，2026-11-10一次66 MB的请求
# 使worker两次堆溢出、整次运行的Trace丢失。按Span顺序分批，单个Span超过上限时独自成批。
MAX_TRACE_REQUEST_BYTES = 5 * 1024 * 1024


def client(
    base_url: str, public_key: str, secret_key: str, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """只接受HTTPS或本机HTTP端点；不读取代理环境变量，凭据只放在认证头里。"""
    endpoint = _base_url(base_url)
    if any(
        not key or len(key) > 8192 or not key.isascii() or any(ord(c) < 33 for c in key)
        for key in (public_key, secret_key)
    ):
        raise EvalkitError("langfuse_credentials_invalid", "Langfuse凭据无效")
    return httpx.Client(
        base_url=endpoint + "/",
        auth=httpx.BasicAuth(public_key, secret_key),
        headers={"x-langfuse-ingestion-version": "4"},
        timeout=httpx.Timeout(10.0),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def _base_url(value: str) -> str:
    try:
        url = urlsplit(value)
        _ = url.port
        local_http = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not url.hostname
            or not (url.scheme == "https" or local_http)
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError
    except ValueError:
        raise EvalkitError("langfuse_endpoint_invalid", "Langfuse端点须为HTTPS或本机HTTP") from None
    return value.rstrip("/")


def _post(http: httpx.Client, path: str, body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        with http.stream("POST", path, json=body) as response:
            if response.status_code not in {200, 207}:
                raise EvalkitError("langfuse_export_rejected", "Langfuse拒绝导出；未自动重试")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ValueError
            return value
    except EvalkitError:
        raise
    except (httpx.HTTPError, ValueError, RecursionError):
        raise EvalkitError("langfuse_export_failed", "Langfuse导出失败；本地报告已保留") from None


def _score_batch(scores: tuple[dict[str, JsonValue], ...]) -> tuple[list[JsonValue], set[str]]:
    batch: list[JsonValue] = []
    ids: set[str] = set()
    for score in scores:
        event_id = str(score["id"])
        ids.add(event_id)
        batch.append(
            {
                "id": event_id,
                "type": "score-create",
                "timestamp": score["timestamp"],
                "body": {key: value for key, value in score.items() if key != "timestamp"},
            }
        )
    return batch, ids


def trace_requests(
    export: LangfuseExport, limit: int = MAX_TRACE_REQUEST_BYTES
) -> list[dict[str, JsonValue]]:
    """把唯一的resourceSpans按Span顺序切成若干OTLP请求，每次请求的JSON正文不超过limit字节。"""

    # 形状由build_export生成并经合同校验：唯一resourceSpans、唯一scopeSpans。
    (resource_spans,) = export.resource_spans
    (scope_spans,) = cast(list[dict[str, JsonValue]], resource_spans["scopeSpans"])
    resource, scope = resource_spans["resource"], scope_spans["scope"]
    spans = cast(list[JsonValue], scope_spans["spans"])

    def request(chunk: list[JsonValue]) -> dict[str, JsonValue]:
        return {
            "resourceSpans": [
                {"resource": resource, "scopeSpans": [{"scope": scope, "spans": chunk}]}
            ]
        }

    overhead = len(json.dumps(request([])).encode())
    requests: list[dict[str, JsonValue]] = []
    chunk: list[JsonValue] = []
    size = overhead
    for span in spans:
        span_bytes = len(json.dumps(span).encode()) + 1
        if chunk and size + span_bytes > limit:
            requests.append(request(chunk))
            chunk, size = [], overhead
        chunk.append(span)
        size += span_bytes
    requests.append(request(chunk))
    return requests


def send(
    export: LangfuseExport,
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """调用方须先获得显式联网授权；失败只影响导出，本地评测结果不变。

    Trace使用OTLP/HTTP JSON，按MAX_TRACE_REQUEST_BYTES分批；全部批次被接受后才发送评分，
    评分使用score-create批处理以保留证据时间。重发使用固定身份、名称和时间，禁止为补发数据重新执行被测系统。
    """
    with client(base_url, public_key, secret_key, transport) as http:
        for body in trace_requests(export, MAX_TRACE_REQUEST_BYTES):
            response = _post(http, "api/public/otel/v1/traces", body)
            partial = response.get("partialSuccess")
            if partial is not None and (
                not isinstance(partial, dict) or str(partial.get("rejectedSpans", "0")) != "0"
            ):
                raise EvalkitError("langfuse_export_partial", "Langfuse拒收部分Trace；未发送评分")
        for offset in range(0, len(export.scores), _SCORE_BATCH_SIZE):
            batch, ids = _score_batch(export.scores[offset : offset + _SCORE_BATCH_SIZE])
            response = _post(http, "api/public/ingestion", {"batch": batch})
            successes = response.get("successes")
            accepted = (
                {
                    str(item.get("id"))
                    for item in successes
                    if isinstance(item, dict) and item.get("status") in (200, 201, 202)
                }
                if isinstance(successes, list)
                else set()
            )
            if response.get("errors") or accepted != ids:
                raise EvalkitError("langfuse_export_partial", "Langfuse评分未全部确认；未自动重试")
