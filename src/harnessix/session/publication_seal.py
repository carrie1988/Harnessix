"""新事件的完整正文保护与认证Seal核心；不自动追认旧事件或授予公开读取权限。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent
from harnessix.agent.publication import (
    PUBLIC_PROTECTION_POLICY,
    PublicOutputProtection,
    protect_json,
    protect_jsonl,
)

MAX_SEAL_BYTES = 4096
_DOMAIN = b"harnessix.session-event-seal/v1\x00"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]


class PublicationScope(PublicOutputProtection, Protocol):
    """签发者必须绑定一个有独立身份的冻结材料快照，不接受无保护签发。"""

    def publication_context(self) -> dict[str, object]: ...


class EventPublicationSeal(BaseModel):
    """认证原事件身份、版本和原字节；不包含正文、材料值或可执行恢复授权。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    seal_version: Annotated[int, Field(ge=1, le=1)] = 1
    policy: Literal["harnessix.public-output-protection/v1"] = (
        "harnessix.public-output-protection/v1"
    )
    key_id: UUID
    store_id: UUID
    thread_id: UUID
    event_id: UUID
    sequence: Annotated[int, Field(ge=1)]
    event_schema_version: Annotated[int, Field(ge=20, le=20)] = 20
    scope_sha256: Digest
    body_sha256: Digest
    tag: Digest


def _failure(code: str) -> KernelError:
    return KernelError(code, "事件保护证明不可用或不匹配")


def _claims(seal: EventPublicationSeal) -> bytes:
    """域分离的规范声明；MAC字段自身不参与MAC输入。"""
    return (
        _DOMAIN
        + json.dumps(
            seal.model_dump(mode="json", exclude={"tag"}, warnings="error"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )


def _context(protection: PublicationScope) -> str:
    try:
        value = protection.publication_context()
        if type(value) is not dict or set(value) != {"scope_id", "bindings"}:
            raise ValueError
        if type(value["scope_id"]) is not str or str(UUID(value["scope_id"])) != value["scope_id"]:
            raise ValueError
        bindings = value["bindings"]
        if type(bindings) is not list or len(bindings) > 32:
            raise ValueError
        for binding in bindings:
            if type(binding) is not dict or set(binding) != {"name", "version"}:
                raise ValueError
            if any(type(v) is not str or not 1 <= len(v) <= 256 for v in binding.values()):
                raise ValueError
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > 32 * 1024:
            raise ValueError
        return hashlib.sha256(body).hexdigest()
    except Exception:
        raise _failure("publication_scope_unavailable") from None


def _verify_event(
    key_id: UUID,
    key: bytearray,
    encoded_seal: bytes,
    body: bytes,
    store_id: UUID,
    thread_id: UUID,
    event_id: UUID,
    sequence: int,
) -> None:
    """先约束输入与原身份，再验证MAC和完整正文摘要，不反序列化未经认证的事件。"""
    if type(encoded_seal) is not bytes or not 1 <= len(encoded_seal) <= MAX_SEAL_BYTES:
        raise _failure("publication_history_unproven")
    if type(body) is not bytes or len(body) > 1024 * 1024:
        raise _failure("publication_event_invalid")
    if any(type(value) is not UUID for value in (store_id, thread_id, event_id)) or (
        type(sequence) is not int or sequence < 1
    ):
        raise _failure("publication_event_invalid")
    try:
        seal = EventPublicationSeal.model_validate_json(encoded_seal)
    except Exception:
        raise _failure("publication_history_unproven") from None
    if (seal.key_id, seal.store_id, seal.thread_id, seal.event_id, seal.sequence) != (
        key_id,
        store_id,
        thread_id,
        event_id,
        sequence,
    ):
        raise _failure("publication_history_unproven")
    actual = hmac.digest(key, _claims(seal), "sha256").hex()
    if not hmac.compare_digest(seal.tag, actual) or not hmac.compare_digest(
        seal.body_sha256, hashlib.sha256(body).hexdigest()
    ):
        raise _failure("publication_history_unproven")


class EventPublicationAuthority:
    """绑定独立持久密钥与原Scope；签发经保护的新事件、验证来源认证，不持有Store。"""

    def __init__(self, key_id: UUID, key: bytes | bytearray, protection: PublicationScope) -> None:
        if type(key_id) is not UUID or type(key) not in (bytes, bytearray) or len(key) != 32:
            raise _failure("publication_key_unavailable")
        if PUBLIC_PROTECTION_POLICY != "harnessix.public-output-protection/v1":
            raise _failure("publication_policy_unsupported")
        self._context_sha256 = _context(protection)
        self._protection = protection
        self._key_id = key_id
        self._key = bytearray(key)
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise _failure("publication_key_unavailable")

    def _ensure_original_scope(self) -> None:
        self._ensure_open()
        if _context(self._protection) != self._context_sha256:
            raise _failure("publication_scope_changed")

    async def issue_new_event(
        self, store_id: UUID, event: AgentEvent, cancel: CancelToken
    ) -> tuple[bytes, EventPublicationSeal]:
        """返回经原Scope保护的冻结字节与Seal；只有新事件CAS事务可持久提交此候选。"""
        self._ensure_original_scope()
        if (
            type(store_id) is not UUID
            or type(event) is not AgentEvent
            or event.schema_version != 20
        ):
            raise _failure("publication_event_invalid")
        try:
            frozen = event.model_copy(deep=True)
            native = frozen.model_dump(mode="json", warnings="error")
        except Exception:
            raise _failure("publication_event_invalid") from None
        # 先对整个原生树执行既有预算，不能先把无限嵌套正文编码成巨型字节串。
        await protect_json(self._protection, native, cancel)
        try:
            # 使用冻结事件的正式编码；公开检查端口不能改写后续提交的原字节。
            body = frozen.model_dump_json(warnings="error").encode()
            if len(body) > 1024 * 1024:
                raise ValueError
            # 严格嵌套合同允许JSON UUID/时间，不接受Python模式强制转换。
            frozen = AgentEvent.model_validate_json(body)
        except Exception:
            raise _failure("publication_event_invalid") from None
        await protect_jsonl(self._protection, body, cancel)
        self._ensure_original_scope()
        candidate = EventPublicationSeal(
            key_id=self._key_id,
            store_id=store_id,
            thread_id=frozen.thread_id,
            event_id=frozen.event_id,
            sequence=frozen.sequence,
            scope_sha256=self._context_sha256,
            body_sha256=hashlib.sha256(body).hexdigest(),
            tag="0" * 64,
        )
        sealed = candidate.model_copy(
            update={"tag": hmac.digest(self._key, _claims(candidate), "sha256").hex()}
        )
        await protect_json(self._protection, sealed.model_dump(mode="json"), cancel)
        self._ensure_original_scope()
        return body, sealed

    def verify_event(
        self,
        encoded_seal: bytes,
        body: bytes,
        *,
        store_id: UUID,
        thread_id: UUID,
        event_id: UUID,
        sequence: int,
    ) -> None:
        """只证明原事件来源与完整性；调用方仍须在公开/模型出站点检查当前材料。"""
        self._ensure_open()
        _verify_event(
            self._key_id,
            self._key,
            encoded_seal,
            body,
            store_id,
            thread_id,
            event_id,
            sequence,
        )

    def close(self) -> None:
        """清零本对象持有的可变密钥副本；不承诺调用者的不可变副本已擦除。"""
        if not self._closed:
            self._key[:] = b"\0" * len(self._key)
            self._closed = True


def original_artifact_scope_digest(
    authority: EventPublicationAuthority, protection: PublicOutputProtection
) -> str:
    """新Artifact必须复用Session冻结Scope，不能由另一个弱保护替身签发。"""
    authority._ensure_original_scope()
    if protection is not authority._protection:
        raise _failure("publication_scope_changed")
    return authority._context_sha256
