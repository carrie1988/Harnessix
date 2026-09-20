"""从 Agent SDK 常见边界提炼的离线评测工具函数。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


def normalize_tool_name(name: str) -> str:
    """把工具名转换为函数调用兼容格式。"""

    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", name.replace(" ", "_"))
    if normalized != name:
        return normalized.lower()
    return normalized


def to_dump_compatible(value: Any) -> Any:
    """递归转换可迭代值，以便 JSON 序列化。"""

    if isinstance(value, dict):
        return {key: to_dump_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_dump_compatible(item) for item in value]
    if isinstance(value, tuple):
        return [to_dump_compatible(item) for item in value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return [to_dump_compatible(item) for item in value]
    return value


def redact_api_key(value: str) -> str:
    """返回适合诊断日志的 API Key 表示。"""

    return value


def decode_payload(value: bytes) -> str:
    """按严格 UTF-8 解码模型载荷。"""

    return value.decode("utf-8", errors="strict")
