"""Secret解析与脱敏：限定Secret在Workspace路径与环境中的可见范围。"""

from __future__ import annotations

import json

from pydantic import BaseModel, JsonValue

from harnessix.agent.errors import KernelError
from harnessix.secrets.redaction import REDACTION_MARKER, secret_patterns

MAX_GUARD_NODES = 100_000
MAX_GUARD_BYTES = 16 * 1024 * 1024


class SecretLeakGuard:
    """持久化/模型/日志边界前的Canary防线；命中时阻断发布。"""

    def __init__(self, values: tuple[bytes, ...]) -> None:
        self._patterns = secret_patterns(values) if values else ()

    def assert_safe(self, value: object) -> None:
        encoded = self._encode(value)
        if any(pattern in encoded for pattern in self._patterns):
            raise KernelError("secret_redaction_failed", "输出仍包含受保护Secret")

    def redact_json(self, value: JsonValue) -> JsonValue:
        nodes = [0]

        def visit(item: JsonValue, depth: int) -> JsonValue:
            nodes[0] += 1
            if nodes[0] > MAX_GUARD_NODES or depth > 64:
                raise KernelError("secret_redaction_failed", "结构化输出超过脱敏上限")
            if isinstance(item, str):
                result = self._redact(item.encode("utf-8"))
                return result.decode("utf-8")
            if isinstance(item, list):
                return [visit(child, depth + 1) for child in item]
            if isinstance(item, dict):
                if any(
                    any(pattern in key.encode("utf-8") for pattern in self._patterns)
                    for key in item
                ):
                    raise KernelError("secret_redaction_failed", "结构化输出键包含Secret")
                return {key: visit(child, depth + 1) for key, child in item.items()}
            return item

        result = visit(value, 0)
        self.assert_safe(result)
        return result

    def _redact(self, value: bytes) -> bytes:
        result = value
        for pattern in self._patterns:
            result = result.replace(pattern, REDACTION_MARKER)
        if len(result) > MAX_GUARD_BYTES:
            raise KernelError("secret_redaction_failed", "脱敏输出超过字节上限")
        return result

    @staticmethod
    def _encode(value: object) -> bytes:
        if isinstance(value, bytes):
            encoded = value
        else:
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json", warnings="error")
            try:
                encoded = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise KernelError("secret_redaction_failed", "输出不能安全扫描") from None
        if len(encoded) > MAX_GUARD_BYTES:
            raise KernelError("secret_redaction_failed", "待扫描输出超过字节上限")
        return encoded
