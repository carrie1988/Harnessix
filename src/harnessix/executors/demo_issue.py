from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import (
    ActionSnapshot,
    EffectReceipt,
    ExecutionOutcome,
    ReconciliationOutcome,
    utc_now,
)


class DemoIssueCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20_000)
    simulate_uncertain_after_commit: bool = False


class DemoIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_id: str
    tenant_id: str
    idempotency_key: str
    title: str
    body: str
    created_at: datetime


class DemoIssueRepository:
    """用独立事务模拟外部 Issue 系统。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        database = await aiosqlite.connect(self.database_path)
        database.row_factory = aiosqlite.Row
        await database.execute("PRAGMA busy_timeout = 5000")
        try:
            yield database
        finally:
            await database.close()

    async def create(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        title: str,
        body: str,
    ) -> DemoIssue:
        existing = await self.find(tenant_id=tenant_id, idempotency_key=idempotency_key)
        if existing is not None:
            return existing

        issue = DemoIssue(
            issue_id=f"ISSUE-{uuid4().hex[:10].upper()}",
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            title=title,
            body=body,
            created_at=utc_now(),
        )
        async with self._connection() as database:
            await database.execute(
                """
                INSERT OR IGNORE INTO demo_issues(
                    issue_id, tenant_id, idempotency_key, title, body, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    issue.issue_id,
                    issue.tenant_id,
                    issue.idempotency_key,
                    issue.title,
                    issue.body,
                    issue.created_at.isoformat(),
                ),
            )
            await database.commit()
        found = await self.find(tenant_id=tenant_id, idempotency_key=idempotency_key)
        assert found is not None
        return found

    async def find(self, *, tenant_id: str, idempotency_key: str) -> DemoIssue | None:
        async with self._connection() as database:
            cursor = await database.execute(
                """
                SELECT * FROM demo_issues
                WHERE tenant_id = ? AND idempotency_key = ?
                """,
                (tenant_id, idempotency_key),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return DemoIssue(
            issue_id=row["issue_id"],
            tenant_id=row["tenant_id"],
            idempotency_key=row["idempotency_key"],
            title=row["title"],
            body=row["body"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class DemoIssueExecutor:
    def __init__(self, repository: DemoIssueRepository) -> None:
        self.repository = repository

    async def execute(self, action: ActionSnapshot, arguments: BaseModel) -> ExecutionOutcome:
        parsed = DemoIssueCreateInput.model_validate(arguments)
        idempotency_key = action.request.idempotency_key
        if idempotency_key is None:
            return ExecutionOutcome.failed(
                code="idempotency_key_required",
                message="demo.issue.create 必须提供 idempotency_key",
            )

        issue = await self.repository.create(
            tenant_id=action.request.principal.tenant_id,
            idempotency_key=idempotency_key,
            title=parsed.title,
            body=parsed.body,
        )
        if parsed.simulate_uncertain_after_commit:
            raise UncertainEffectError("模拟外部 Issue 已提交，但执行结果在持久化前丢失")
        return ExecutionOutcome.succeeded(
            output=issue.model_dump(mode="json"),
            receipt=self._receipt(issue),
        )

    async def reconcile(self, action: ActionSnapshot) -> ReconciliationOutcome:
        idempotency_key = action.request.idempotency_key
        if idempotency_key is None:
            return ReconciliationOutcome.manual(
                code="missing_reconciliation_key",
                message="缺少业务幂等键，无法自动对账",
            )
        issue = await self.repository.find(
            tenant_id=action.request.principal.tenant_id,
            idempotency_key=idempotency_key,
        )
        if issue is None:
            return ReconciliationOutcome.failed(
                code="effect_not_found",
                message="外部系统未找到对应 Issue，可确认副作用没有提交",
            )
        return ReconciliationOutcome.succeeded(
            output=issue.model_dump(mode="json"),
            receipt=self._receipt(issue),
        )

    @staticmethod
    def _receipt(issue: DemoIssue) -> EffectReceipt:
        return EffectReceipt(
            provider="demo-issue-system",
            resource_type="issue",
            resource_id=issue.issue_id,
            idempotency_key=issue.idempotency_key,
        )
