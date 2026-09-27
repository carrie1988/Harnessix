"""Artifact正文保护与持久来源认证；当前Scope仍须独立重验。"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import aiosqlite

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import (
    PUBLIC_PROTECTION_POLICY,
    PublicOutputProtection,
    protect_binary_jsonl,
    protect_jsonl,
)
from harnessix.artifacts.binary_projection import decode_process_artifact
from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES
from harnessix.session.store_publication import (
    EMPTY_PREFIX,
    ArtifactPublicationSeal,
    SessionPublicationBinding,
)


class ArtifactPublicationGuard:
    def __init__(
        self,
        protection: PublicOutputProtection | None,
        binding: SessionPublicationBinding | None = None,
    ) -> None:
        if binding is not None and protection is None:
            raise KernelError("publication_scope_unavailable", "Artifact认证发布缺少保护作用域")
        self.protection = protection
        self.binding = binding
        self.epoch = str(uuid4()) if protection is not None else None

    @property
    def policy(self) -> str | None:
        return PUBLIC_PROTECTION_POLICY if self.protection is not None else None

    async def check_body(self, body: bytes, *, purpose: str = "tool_result") -> None:
        if self.binding is not None:
            assert self.protection is not None
            self.binding.artifact.scope_digest(self.protection)
        if purpose in {"action_output", "process_output"}:
            await protect_binary_jsonl(
                self.protection,
                body,
                lambda data, checkpoint: decode_process_artifact(data, purpose, checkpoint),
                CancelToken(),
            )
        else:
            await protect_jsonl(self.protection, body, CancelToken())
        if self.binding is not None:
            assert self.protection is not None
            self.binding.artifact.scope_digest(self.protection)

    def require_proof(self, row: aiosqlite.Row) -> None:
        if self.binding is not None:
            if row["state"] == "expired":
                return
            try:
                claims = artifact_claims(row, self.binding)
                self.binding.artifact.verify(row["publication_seal"], claims)
            except (KeyError, TypeError, ValueError, KernelError):
                raise KernelError(
                    "artifact_publication_unproven", "Artifact缺少匹配的持久来源认证"
                ) from None
            return
        if self.protection is not None and (
            row["publication_epoch"] != self.epoch or row["publication_policy"] != self.policy
        ):
            raise KernelError("artifact_publication_unproven", "Artifact缺少当前运行保护证明")

    def issue_proof(self, row: aiosqlite.Row) -> bytes | None:
        """仅对已完成持久前保护的原行签发；调用者在同一事务内写回。"""
        if self.binding is None:
            return None
        assert self.protection is not None
        return self.binding.artifact.issue(artifact_claims(row, self.binding), self.protection)


def artifact_claims(
    row: aiosqlite.Row, binding: SessionPublicationBinding
) -> ArtifactPublicationSeal:
    """从有界原行构造待签/待验声明；不得规范化旧Manifest后再签。"""
    body, manifest = row["body"], row["manifest_json"]
    if (
        type(body) is not bytes
        or len(body) > MAX_ARTIFACT_BYTES
        or type(manifest) is not str
        or len(manifest) > 8192
        or len(manifest.encode()) > 8192
        or type(row["size_bytes"]) is not int
        or row["size_bytes"] != len(body)
        or row["state"] != "published"
    ):
        raise KernelError("artifact_publication_unproven", "Artifact原行超限或状态不匹配")

    def identity(name: str) -> UUID:
        raw = row[name]
        value = UUID(raw)
        if type(raw) is not str or str(value) != raw:
            raise ValueError("Artifact身份不规范")
        return value

    store_id, key_id = binding.artifact.identity()
    return ArtifactPublicationSeal(
        store_id=store_id,
        key_id=key_id,
        tag=EMPTY_PREFIX,
        artifact_id=identity("artifact_id"),
        thread_id=identity("thread_id"),
        turn_id=identity("turn_id"),
        call_id=identity("call_id"),
        workspace_scope=row["workspace_scope"],
        artifact_purpose=row["purpose"],
        publication_epoch=identity("publication_epoch"),
        publication_policy=row["publication_policy"],
        scope_sha256=EMPTY_PREFIX,
        manifest_sha256=hashlib.sha256(manifest.encode()).hexdigest(),
        body_sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=row["size_bytes"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )
