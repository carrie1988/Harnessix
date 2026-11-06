"""显式发送已生成的只读投影；不调度Agent、不自动重试、不回传响应正文。"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.evals.langfuse_contracts import LangfuseExport

_MAX_RESPONSE_BYTES = 65_536
_SCORE_BATCH_SIZE = 50


def _client(
    base_url: str, public_key: str, secret_key: str, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    endpoint = _base_url(base_url)
    if any(
        not key or len(key) > 8192 or not key.isascii() or any(ord(c) < 33 for c in key)
        for key in (public_key, secret_key)
    ):
        raise KernelError("langfuse_credentials_invalid", "Langfuse导出凭据无效")
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
        raise KernelError(
            "langfuse_endpoint_invalid", "Langfuse端点必须使用HTTPS或本机HTTP"
        ) from None
    return value.rstrip("/")


def _post(client: httpx.Client, path: str, body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        with client.stream("POST", path, json=body) as response:
            if response.status_code not in {200, 207}:
                raise KernelError("langfuse_export_rejected", "Langfuse拒绝导出；未自动重试")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ValueError
            return value
    except KernelError:
        raise
    except (httpx.HTTPError, ValueError, RecursionError):
        raise KernelError("langfuse_export_failed", "Langfuse导出失败；本地报告已保留") from None


def send_langfuse_suite_export(
    export: LangfuseExport,
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """调用前必须获得显式网络授权；失败仅影响导出，原始评测结果不变。

    Trace使用OTLP/HTTP JSON，评分使用仍受支持的score-create批处理以保留事件时间。
    不跟随重定向；重发使用固定身份、名称和证据时间，禁止重新执行Agent来补发数据。
    """

    with _client(base_url, public_key, secret_key, transport) as client:
        response = _post(
            client, "api/public/otel/v1/traces", {"resourceSpans": list(export.resource_spans)}
        )
        partial = response.get("partialSuccess")
        if partial is not None and (
            not isinstance(partial, dict) or str(partial.get("rejectedSpans", "0")) != "0"
        ):
            raise KernelError("langfuse_export_partial", "Langfuse拒收部分Trace；未发送评分")
        for offset in range(0, len(export.scores), _SCORE_BATCH_SIZE):
            batch: list[JsonValue] = []
            ids: set[str] = set()
            for score in export.scores[offset : offset + _SCORE_BATCH_SIZE]:
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
            response = _post(client, "api/public/ingestion", {"batch": batch})
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
                raise KernelError("langfuse_export_partial", "Langfuse评分未全部确认；未自动重试")
