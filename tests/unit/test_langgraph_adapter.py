from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel

from harnessix.adapters.langgraph import HarnessixToolContext, create_harnessix_tool
from harnessix.domain.models import (
    ActionContext,
    ActionRequest,
    ActionSnapshot,
    ActionStatus,
    EffectClass,
    Principal,
    ToolDescriptor,
)


class Input(BaseModel):
    title: str


class FakeAsyncClient:
    def __init__(self) -> None:
        self.requests: list[ActionRequest] = []

    async def submit(self, request: ActionRequest) -> ActionSnapshot:
        self.requests.append(request)
        now = datetime.now(UTC)
        return ActionSnapshot(
            request=request,
            request_fingerprint="fingerprint",
            tool=ToolDescriptor(
                name=request.tool,
                version="1.0.0",
                description="test",
                input_schema={},
                effect_class=EffectClass.IDEMPOTENT_WRITE,
                risk_level="medium",
                requires_idempotency=True,
                requires_approval=True,
                supports_reconciliation=True,
            ),
            status=ActionStatus.PENDING_APPROVAL,
            created_at=now,
            updated_at=now,
            version=1,
        )


async def test_langgraph_tool_builds_framework_neutral_action() -> None:
    client = FakeAsyncClient()
    tool = create_harnessix_tool(
        action_name="demo.issue.create",
        description="创建 Issue",
        args_schema=Input,
        async_client=client,
        context=HarnessixToolContext(
            principal=Principal(tenant_id="tenant", subject_id="agent", framework="langgraph"),
            action_context=ActionContext(session_id="thread", run_id="run"),
        ),
        effect_hint=EffectClass.IDEMPOTENT_WRITE,
        idempotency_key=lambda arguments: f"issue:{arguments['title']}",
    )

    result = json.loads(await tool.ainvoke({"title": "故障"}))

    assert result["status"] == "pending_approval"
    assert client.requests[0].principal.framework == "langgraph"
    assert client.requests[0].idempotency_key == "issue:故障"
    assert client.requests[0].metadata["adapter"] == "langgraph"
