from __future__ import annotations

import asyncio
import os

from harnessix import (
    ActionContext,
    ActionRequest,
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    HarnessixAsyncClient,
    Principal,
)


def request(tool: str, arguments: dict[str, object], *, key: str | None = None) -> ActionRequest:
    return ActionRequest(
        tool=tool,
        arguments=arguments,
        principal=Principal(tenant_id="demo", subject_id="mvp-agent", framework="custom"),
        context=ActionContext(session_id="mvp-session", run_id="mvp-run"),
        idempotency_key=key,
        effect_hint=(EffectClass.IDEMPOTENT_WRITE if key is not None else EffectClass.READ_ONLY),
    )


async def main() -> None:
    base_url = os.getenv("HARNESSIX_BASE_URL", "http://127.0.0.1:8787")
    async with HarnessixAsyncClient(base_url) as client:
        echo = await client.submit(request("system.echo", {"message": "你好 Harnessix"}))
        print(f"1. 只读 Action：{echo.status}")

        issue_request = request(
            "demo.issue.create",
            {
                "title": "演示不确定副作用",
                "body": "外部资源已创建，但本地结果丢失",
                "simulate_uncertain_after_commit": True,
            },
            key="mvp:uncertain-issue",
        )
        pending = await client.submit(issue_request)
        print(f"2. 提交写 Action：{pending.status}")

        unknown = await client.decide_approval(
            issue_request.action_id,
            ApprovalDecision(
                outcome=ApprovalOutcome.APPROVED,
                actor="mvp-reviewer",
                reason="批准故障注入演示",
            ),
        )
        print(f"3. 外部提交后结果丢失：{unknown.status}")

        reconciled = await client.reconcile(issue_request.action_id)
        resource_id = (
            reconciled.result.receipt.resource_id
            if reconciled.result and reconciled.result.receipt
            else "无"
        )
        print(f"4. 对账完成：{reconciled.status}，外部资源：{resource_id}")

        duplicate = await client.submit(
            request(
                "demo.issue.create",
                {
                    "title": "演示不确定副作用",
                    "body": "外部资源已创建，但本地结果丢失",
                    "simulate_uncertain_after_commit": True,
                },
                key="mvp:uncertain-issue",
            )
        )
        print(f"5. 重复提交复用原 Action：{duplicate.request.action_id == issue_request.action_id}")


if __name__ == "__main__":
    asyncio.run(main())
