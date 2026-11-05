"""显式启用的模型端点在线探测：可达、模型存在、工具调用参数可解析。

只用于`harnessix code doctor --probe-model`；启动预检保持离线。探测请求很小（一次模型列表、
一次带单个工具的补全），不携带工作区内容；API Key只放在请求头，不进入结果。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from harnessix.models.config import LOOPBACK_HOSTS

ProbeStatus = Literal["passed", "warning", "failed"]

_TOOL = {
    "type": "function",
    "function": {
        "name": "record_items",
        "description": "Record the requested items.",
        "parameters": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        },
    },
}
_PROMPT = 'Call record_items with items ["alpha", "beta"]. Do not answer in text.'


@dataclass(frozen=True)
class EndpointProbe:
    check: str
    status: ProbeStatus
    detail: str
    seconds: float


def _request(url: str, api_key: str, body: dict[str, Any] | None, timeout: float) -> Any:
    # 回环端点不经代理（与产品Provider一致），远端端点沿用环境代理设置。
    loopback = urlsplit(url).hostname in LOOPBACK_HOSTS
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if loopback
        else urllib.request.build_opener()
    )
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read())


def probe_chat_endpoint(
    base_url: str,
    model: str,
    api_key: str,
    *,
    extra_body: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> list[EndpointProbe]:
    """依次探测模型列表与工具调用；前一步失败时不再继续。"""

    base = base_url.rstrip("/")
    results: list[EndpointProbe] = []
    started = time.monotonic()
    try:
        listed = _request(f"{base}/models", api_key, None, timeout)
        names = {item.get("id") for item in listed.get("data", []) if isinstance(item, dict)}
    except (OSError, ValueError, urllib.error.URLError) as error:
        results.append(
            EndpointProbe("endpoint", "failed", type(error).__name__, time.monotonic() - started)
        )
        return results
    found = model in names
    results.append(
        EndpointProbe(
            "model",
            "passed" if found else "failed",
            "已列出" if found else f"端点未列出该模型（共{len(names)}个）",
            time.monotonic() - started,
        )
    )
    if not found:
        return results

    started = time.monotonic()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT}],
        "tools": [_TOOL],
        "max_tokens": 256,
        "temperature": 0,
        **(extra_body or {}),
    }
    try:
        reply = _request(f"{base}/chat/completions", api_key, body, timeout)
        calls = reply["choices"][0]["message"].get("tool_calls") or []
    except (OSError, ValueError, KeyError, IndexError, urllib.error.URLError) as error:
        results.append(
            EndpointProbe("tool_call", "failed", type(error).__name__, time.monotonic() - started)
        )
        return results
    seconds = time.monotonic() - started
    if not calls:
        results.append(EndpointProbe("tool_call", "failed", "模型未发起工具调用", seconds))
        return results
    try:
        arguments = json.loads(calls[0]["function"]["arguments"])
    except (KeyError, TypeError, ValueError):
        results.append(EndpointProbe("tool_call", "failed", "工具参数不是合法JSON", seconds))
        return results
    items = arguments.get("items") if isinstance(arguments, dict) else None
    if isinstance(items, list):
        results.append(EndpointProbe("tool_call", "passed", "数组参数类型正确", seconds))
    elif isinstance(items, str):
        # 端点把数组编码成JSON字符串：需要Provider开启normalize_tool_arguments。
        results.append(
            EndpointProbe(
                "tool_call", "warning", "数组参数被编码成字符串，需开启工具参数还原", seconds
            )
        )
    else:
        results.append(EndpointProbe("tool_call", "failed", "工具参数缺少items", seconds))
    return results
