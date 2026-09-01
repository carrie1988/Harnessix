from __future__ import annotations

from harnessix.domain.models import EffectClass, RiskLevel
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
from harnessix.storage import SQLiteEffectJournal


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
    issue_repository = DemoIssueRepository(settings.database_path)
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


def build_service(settings: Settings) -> ActionService:
    return ActionService(
        journal=SQLiteEffectJournal(settings.database_path),
        registry=build_registry(settings),
        policy_engine=DefaultPolicyEngine(),
        lease_seconds=settings.lease_seconds,
    )
