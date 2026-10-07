"""同次完整认证读取保留的原正文定位；普通事实，不是认证或执行能力。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EventBodyRef:
    """绑定原事件身份与原 UTF-8 字节摘要，不保存正文、Seal、Key 或 Store。"""

    thread_id: UUID
    event_id: UUID
    sequence: int
    body_sha256: str
