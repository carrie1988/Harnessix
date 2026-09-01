from __future__ import annotations

from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.domain.ports import EffectJournal
from harnessix.domain.registry import ToolDefinition, ToolRegistry
from harnessix.executors import (
    DemoIssueCreateInput,
    DemoIssueExecutor,
    DemoIssueRepository,
    EchoExecutor,
    EchoInput,
)
from harnessix.policy import DefaultPolicyEngine
from harnessix.runtime import ActionService
from harnessix.settings import Settings
from harnessix.storage import PostgresEffectJournal, SQLiteEffectJournal


def build_registry(settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="system.echo",
            version="1.0.0",
            description="返回输入消息，用于验证只读 Action 执行链路",
            input_model=EchoInput,
            effect_class=EffectClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            executor=EchoExecutor(),
        )
    )
    issue_repository = DemoIssueRepository(settings.demo_database_path)
    registry.register(
        ToolDefinition(
            name="demo.issue.create",
            version="1.0.0",
            description="创建可审批、可幂等、可对账的演示 Issue",
            input_model=DemoIssueCreateInput,
            effect_class=EffectClass.IDEMPOTENT_WRITE,
            risk_level=RiskLevel.MEDIUM,
            executor=DemoIssueExecutor(issue_repository),
            requires_idempotency=True,
            requires_approval=True,
            supports_reconciliation=True,
        )
    )
    return registry


def build_journal(settings: Settings) -> EffectJournal:
    if settings.database_url is not None:
        return PostgresEffectJournal(settings.database_url)
    return SQLiteEffectJournal(settings.database_path)


def build_service(settings: Settings, *, worker_id: str | None = None) -> ActionService:
    return ActionService(
        journal=build_journal(settings),
        registry=build_registry(settings),
        policy_engine=DefaultPolicyEngine(),
        lease_seconds=settings.lease_seconds,
        worker_id=worker_id,
        auto_execute=settings.execution_mode == "inline",
    )
