from __future__ import annotations

from pydantic import BaseModel

from harnessix import ActionContext, EffectClass, HarnessixAsyncClient, Principal
from harnessix.adapters.langgraph import HarnessixToolContext, create_harnessix_tool


class IssueInput(BaseModel):
    title: str
    body: str = ""


client = HarnessixAsyncClient()
issue_tool = create_harnessix_tool(
    action_name="demo.issue.create",
    description="创建经过 Harnessix 治理的 Issue",
    args_schema=IssueInput,
    async_client=client,
    context=HarnessixToolContext(
        principal=Principal(tenant_id="demo", subject_id="langgraph-agent", framework="langgraph"),
        action_context=ActionContext(session_id="thread-1", run_id="run-1"),
    ),
    effect_hint=EffectClass.IDEMPOTENT_WRITE,
    idempotency_key=lambda arguments: f"issue:{arguments['title']}",
)

print(f"已创建 LangGraph Tool：{issue_tool.name}")
