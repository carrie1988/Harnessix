"""Session逻辑身份、认证事件/投影和Artifact正文；来源认证不授予公开许可。"""

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
from harnessix.agent.publication import PublicOutputProtection
from harnessix.session.publication_seal import (
    Digest,
    EventPublicationAuthority,
    PublicationScope,
    original_artifact_scope_digest,
)

EMPTY_PREFIX = "0" * 64
MAX_PROJECTION_BYTES = 64 * 1024 * 1024
MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_HISTORY_EVENTS = 100_000
_DOMAIN = b"harnessix.session-store-publication/v1\x00"
_ARTIFACT_DOMAIN = b"harnessix.artifact-publication/v1\x00"


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


class ArtifactPublicationSeal(_Identity):
    """认证原始Artifact行身份、正文和Manifest；不授予当前公开读取权限。"""

    purpose: Literal["artifact_body"] = "artifact_body"
    artifact_id: UUID
    thread_id: UUID
    turn_id: UUID
    call_id: UUID
    workspace_scope: Digest
    artifact_purpose: Literal[
        "tool_result",
        "batch_plan",
        "batch_effect",
        "process_output",
        "action_review",
        "action_output",
    ]
    publication_epoch: UUID
    publication_policy: Literal["harnessix.public-output-protection/v1"]
    scope_sha256: Digest
    manifest_sha256: Digest
    body_sha256: Digest
    size_bytes: Annotated[int, Field(ge=0, le=1024 * 1024)]
    expires_at: Annotated[str, Field(min_length=1, max_length=64)]
    created_at: Annotated[str, Field(min_length=1, max_length=64)]


def _claims(value: _Identity, domain: bytes = _DOMAIN) -> bytes:
    return (
        domain
        + json.dumps(
            value.model_dump(mode="json", exclude={"tag"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )


def _signed[T: _Identity](value: T, key: bytearray, domain: bytes = _DOMAIN) -> bytes:
    return (
        value.model_copy(update={"tag": hmac.digest(key, _claims(value, domain), "sha256").hex()})
        .model_dump_json()
        .encode()
    )


def _verified[T: _Identity](
    kind: type[T],
    body: object,
    key: bytearray,
    store_id: UUID,
    key_id: UUID,
    domain: bytes = _DOMAIN,
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
        or not hmac.compare_digest(
            value.tag, hmac.digest(key, _claims(value, domain), "sha256").hex()
        )
    ):
        raise unproven()
    return value


class ArtifactPublicationAuthority:
    """只处理Artifact签发与验真；共享Binding密钥生命周期，不另复制密钥。"""

    def __init__(self, binding: SessionPublicationBinding) -> None:
        self._binding = binding

    def identity(self) -> tuple[UUID, UUID]:
        self._binding._ensure_open()
        return self._binding._store_id, self._binding._key_id

    def scope_digest(self, protection: PublicOutputProtection) -> str:
        self._binding._ensure_open()
        return original_artifact_scope_digest(self._binding._events, protection)

    def issue(self, claims: ArtifactPublicationSeal, protection: PublicOutputProtection) -> bytes:
        if (claims.store_id, claims.key_id) != self.identity():
            raise unproven()
        scope = self.scope_digest(protection)
        return _signed(
            claims.model_copy(update={"scope_sha256": scope}),
            self._binding._key,
            _ARTIFACT_DOMAIN,
        )

    def verify(self, body: object, claims: ArtifactPublicationSeal) -> None:
        store_id, key_id = self.identity()
        actual = _verified(
            ArtifactPublicationSeal,
            body,
            self._binding._key,
            store_id,
            key_id,
            _ARTIFACT_DOMAIN,
        )
        if actual.model_dump(exclude={"tag", "scope_sha256"}) != claims.model_dump(
            exclude={"tag", "scope_sha256"}
        ):
            raise unproven()


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
        self.artifact = ArtifactPublicationAuthority(self)

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
