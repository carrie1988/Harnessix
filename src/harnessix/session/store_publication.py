"""Session逻辑身份与认证事件前缀派生投影；不把来源认证当作当前公开许可。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, Thread
from harnessix.session.publication_seal import (
    Digest,
    EventPublicationAuthority,
    PublicationScope,
)

EMPTY_PREFIX = "0" * 64
MAX_PROJECTION_BYTES = 64 * 1024 * 1024
MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_HISTORY_EVENTS = 100_000
_DOMAIN = b"harnessix.session-store-publication/v1\x00"


def unproven() -> KernelError:
    return KernelError("publication_history_unproven", "Session历史缺少匹配的来源认证")


def original_bytes(value: object, maximum: int) -> bytes:
    """先限制原文本长度再编码；数据库输入不接受隐式类型转换。"""
    if type(value) is not str or len(value) > maximum:
        raise unproven()
    body = value.encode("utf-8")
    if len(body) > maximum:
        raise unproven()
    return body


def extend_prefix(previous: str, encoded_event_seal: bytes) -> str:
    """无密钥链摘要只由认证Checkpoint赋予信任，单独存储值不构成证明。"""
    return hashlib.sha256(
        bytes.fromhex(previous) + hashlib.sha256(encoded_event_seal).digest()
    ).hexdigest()


class _Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Annotated[int, Field(ge=1, le=1)] = 1
    purpose: str
    store_id: UUID
    key_id: UUID
    tag: Digest


class StoreIdentitySeal(_Identity):
    """认证逻辑Store身份；不代表物理文件、Tenant或不可回滚目录。"""

    purpose: Literal["store_identity"] = "store_identity"


class ProjectionPublicationSeal(_Identity):
    """可信Reducer的原投影字节与已认证事件前缀；不重扫聚合并补签旧材料。"""

    purpose: Literal["derived_projection"] = "derived_projection"
    thread_id: UUID
    sequence: Annotated[int, Field(ge=1, le=MAX_HISTORY_EVENTS)]
    history_bytes: Annotated[int, Field(ge=1, le=MAX_HISTORY_BYTES)]
    projection_version: Annotated[int, Field(ge=20, le=20)] = 20
    prefix_sha256: Digest
    snapshot_sha256: Digest


def _claims(value: _Identity) -> bytes:
    return (
        _DOMAIN
        + json.dumps(
            value.model_dump(mode="json", exclude={"tag"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )


def _signed[T: _Identity](value: T, key: bytearray) -> bytes:
    return (
        value.model_copy(update={"tag": hmac.digest(key, _claims(value), "sha256").hex()})
        .model_dump_json()
        .encode()
    )


def _verified[T: _Identity](
    kind: type[T], body: object, key: bytearray, store_id: UUID, key_id: UUID
) -> T:
    if type(body) is not bytes or not 1 <= len(body) <= 4096:
        raise unproven()
    try:
        value = kind.model_validate_json(body)
    except Exception:
        raise unproven() from None
    if (
        value.store_id != store_id
        or value.key_id != key_id
        or not hmac.compare_digest(value.tag, hmac.digest(key, _claims(value), "sha256").hex())
    ):
        raise unproven()
    return value


class SessionPublicationBinding:
    """宿主拥有稳定逻辑身份和独立密钥；Store拥有同事务事实，不持有模型凭据。"""

    def __init__(
        self, store_id: UUID, key_id: UUID, key: bytes | bytearray, protection: PublicationScope
    ) -> None:
        if type(store_id) is not UUID:
            raise unproven()
        self._events = EventPublicationAuthority(key_id, key, protection)
        self._store_id, self._key_id = store_id, key_id
        self._key = bytearray(key)
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise KernelError("publication_key_unavailable", "Session认证密钥不可用")

    def header(self) -> bytes:
        self._ensure_open()
        return _signed(
            StoreIdentitySeal(store_id=self._store_id, key_id=self._key_id, tag=EMPTY_PREFIX),
            self._key,
        )

    def verify_header(self, body: object) -> None:
        self._ensure_open()
        _verified(StoreIdentitySeal, body, self._key, self._store_id, self._key_id)

    async def issue_event(self, event: AgentEvent) -> tuple[bytes, bytes]:
        self._ensure_open()
        body, seal = await self._events.issue_new_event(self._store_id, event, CancelToken())
        return body, seal.model_dump_json().encode()

    def verify_event(
        self, seal: object, body: bytes, event_id: UUID, thread_id: UUID, sequence: int
    ) -> None:
        self._ensure_open()
        if type(seal) is not bytes:
            raise unproven()
        self._events.verify_event(
            seal,
            body,
            store_id=self._store_id,
            thread_id=thread_id,
            event_id=event_id,
            sequence=sequence,
        )

    def projection(self, thread: Thread, encoded: str, prefix: str, history_bytes: int) -> bytes:
        self._ensure_open()
        return _signed(
            ProjectionPublicationSeal(
                store_id=self._store_id,
                key_id=self._key_id,
                thread_id=thread.thread_id,
                sequence=thread.sequence,
                prefix_sha256=prefix,
                history_bytes=history_bytes,
                snapshot_sha256=hashlib.sha256(
                    original_bytes(encoded, MAX_PROJECTION_BYTES)
                ).hexdigest(),
                tag=EMPTY_PREFIX,
            ),
            self._key,
        )

    def verify_projection(self, body: object, thread_id: UUID) -> ProjectionPublicationSeal:
        self._ensure_open()
        value = _verified(ProjectionPublicationSeal, body, self._key, self._store_id, self._key_id)
        if value.thread_id != thread_id:
            raise unproven()
        return value

    def close(self) -> None:
        if not self._closed:
            self._events.close()
            self._key[:] = b"\0" * len(self._key)
            self._closed = True
