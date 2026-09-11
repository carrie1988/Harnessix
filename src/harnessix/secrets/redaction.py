"""Secret解析与脱敏：在输出、异常和审计边界替换Secret字节模式。"""

from __future__ import annotations

import base64
import json
import shlex
from urllib.parse import quote_from_bytes

from harnessix.agent.errors import KernelError

REDACTION_MARKER = b"[REDACTED]"
MIN_REDACTABLE_SECRET_BYTES = 4
MAX_REDACTION_PATTERNS = 256


def secret_patterns(values: tuple[bytes, ...]) -> tuple[bytes, ...]:
    patterns: set[bytes] = set()
    for value in values:
        if len(value) < MIN_REDACTABLE_SECRET_BYTES:
            raise KernelError("secret_redaction_unsafe", "Secret过短，无法安全脱敏")
        patterns.add(value)
        encoded_base64 = base64.b64encode(value)
        encoded_urlsafe = base64.urlsafe_b64encode(value)
        patterns.update(
            {
                encoded_base64,
                encoded_base64.rstrip(b"="),
                encoded_urlsafe,
                encoded_urlsafe.rstrip(b"="),
                value.hex().encode("ascii"),
                value.hex().upper().encode("ascii"),
            }
        )
        encoded = quote_from_bytes(value, safe="").encode("ascii")
        patterns.add(encoded)
        patterns.add(encoded.lower())
        try:
            text = value.decode("utf-8")
            patterns.add(json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8"))
            patterns.add(shlex.quote(text).encode("utf-8"))
        except UnicodeError:
            pass
    patterns.discard(b"")
    if len(patterns) > MAX_REDACTION_PATTERNS:
        raise KernelError("secret_redaction_unsafe", "Secret脱敏模式超过上限")
    return tuple(sorted(patterns, key=lambda item: (-len(item), item)))


class StreamingSecretRedactor:
    """保留最长模式窗口，确保跨输出块的Secret不会提前发布。"""

    def __init__(self, values: tuple[bytes, ...]) -> None:
        self._patterns = secret_patterns(values) if values else ()
        self._maximum = max((len(item) for item in self._patterns), default=1)
        self._buffer = bytearray()
        self._closed = False

    def feed(self, data: bytes) -> bytes:
        if self._closed:
            raise KernelError("secret_redaction_closed", "Secret脱敏器已经关闭")
        if type(data) is not bytes:
            raise KernelError("secret_redaction_failed", "Secret脱敏输入必须是bytes")
        if not self._patterns:
            return data
        self._buffer.extend(data)
        return self._drain(final=False)

    def finish(self) -> bytes:
        if self._closed:
            return b""
        self._closed = True
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> bytes:
        output = bytearray()
        while self._buffer and (final or len(self._buffer) >= self._maximum):
            matched = next(
                (pattern for pattern in self._patterns if self._buffer.startswith(pattern)), None
            )
            if matched is not None:
                del self._buffer[: len(matched)]
                output.extend(REDACTION_MARKER)
            else:
                output.append(self._buffer[0])
                del self._buffer[0]
        return bytes(output)


def redact_bytes(data: bytes, values: tuple[bytes, ...]) -> bytes:
    redactor = StreamingSecretRedactor(values)
    return redactor.feed(data) + redactor.finish()
