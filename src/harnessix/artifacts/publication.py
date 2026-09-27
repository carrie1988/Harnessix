"""当前宿主保护证明：同事务写入Epoch，不以当前值授权旧Artifact正文。"""

from __future__ import annotations

from uuid import uuid4

import aiosqlite

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import (
    PUBLIC_PROTECTION_POLICY,
    PublicOutputProtection,
    protect_jsonl,
)


class ArtifactPublicationGuard:
    def __init__(self, protection: PublicOutputProtection | None) -> None:
        self.protection = protection
        self.epoch = str(uuid4()) if protection is not None else None

    @property
    def policy(self) -> str | None:
        return PUBLIC_PROTECTION_POLICY if self.protection is not None else None

    async def check_body(self, body: bytes) -> None:
        await protect_jsonl(self.protection, body, CancelToken())

    def require_proof(self, row: aiosqlite.Row) -> None:
        if self.protection is not None and (
            row["publication_epoch"] != self.epoch or row["publication_policy"] != self.policy
        ):
            raise KernelError("artifact_publication_unproven", "Artifact缺少当前运行保护证明")
