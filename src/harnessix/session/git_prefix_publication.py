"""独立Git尾锚签发和只验真；不解析私有catalog、不提供存储或执行入口。"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.publication import PublicOutputProtection, protect_json
from harnessix.session import store_publication as codec
from harnessix.session.git_prefix_contracts import (
    GitStorePrefixAnchorClaims,
    snapshot_git_store_prefix_claims,
)

_DOMAIN = b"harnessix.git-store-prefix-anchor/v1\0"


class GitStorePrefixVerifier:
    """共享原Binding Key及关闭状态；只验历史原MAC，不读取当前Scope或签发。"""

    def __init__(self, binding: codec.SessionPublicationBinding) -> None:
        self._binding = binding

    def identity(self) -> tuple[UUID, UUID]:
        self._binding._ensure_open()
        return self._binding._store_id, self._binding._key_id

    def verify(
        self,
        seal: object,
        claims: GitStorePrefixAnchorClaims,
        body: object,
        *,
        checkpoint: Callable[[], None],
    ) -> None:
        """身份/MAC及新用途规范编码先于正文观察；不补签、不解析或截断catalog。"""
        store_id, key_id = self.identity()
        expected = snapshot_git_store_prefix_claims(claims)
        actual = codec._verified(
            codec.GitStorePrefixAnchorSeal,
            seal,
            self._binding._key,
            store_id,
            key_id,
            _DOMAIN,
        )
        # 新用途只接受签发器的唯一编码；重复键、别名编码、空白和重排均不追认。
        if seal != actual.model_dump_json().encode() or actual.claims != expected:
            raise codec.unproven()
        if type(body) is not bytes or len(body) != actual.body_bytes:
            raise codec.unproven()
        if not hmac.compare_digest(actual.body_sha256, codec._git_body_digest(body, checkpoint)):
            raise codec.unproven()
        self._binding._ensure_open()


class GitStorePrefixAuthority(GitStorePrefixVerifier):
    """仅受信有限Writer持有；使用原Key/Scope保护新Seal，不签发任意JSON用途。"""

    async def issue(
        self,
        claims: GitStorePrefixAnchorClaims,
        body: object,
        protection: PublicOutputProtection,
        *,
        cancel: CancelToken,
    ) -> bytes:
        """冻结并认证完整私有字节；经原低敏Seal保护及最终关闭/取消复核才返回。"""
        store_id, key_id = self.identity()
        frozen = snapshot_git_store_prefix_claims(claims)
        scope = self._binding.artifact.scope_digest(protection)
        digest = codec._git_body_digest(body, cancel.checkpoint)
        self._binding._ensure_open()
        assert isinstance(body, bytes)
        candidate = codec.GitStorePrefixAnchorSeal(
            store_id=store_id,
            key_id=key_id,
            tag=codec.EMPTY_PREFIX,
            claims=frozen,
            scope_sha256=scope,
            body_sha256=digest,
            body_bytes=len(body),
        )
        encoded = codec._signed(candidate, self._binding._key, _DOMAIN)
        await protect_json(
            protection,
            codec.GitStorePrefixAnchorSeal.model_validate_json(encoded).model_dump(mode="json"),
            cancel,
        )
        self._binding.artifact.scope_digest(protection)
        cancel.checkpoint()
        # 最终取消回调仍可能撤销原Scope，交付前必须再检查实际对象。
        self._binding.artifact.scope_digest(protection)
        self._binding._ensure_open()
        return encoded
