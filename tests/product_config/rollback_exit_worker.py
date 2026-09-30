"""默认产品真实硬退出工作进程；只使用固定离线Provider和测试凭据。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.delivery import filesystem
from harnessix.product_config.server import run_product_stdio
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    PublicApprovalDecision,
    PublicApprovalRequestContent,
    PublicToolResultContent,
)
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _proposal
from tests.product_config.test_product_patch_rollback import rollback_step
from tests.product_config.test_product_rollback_sdk import (
    ProductBundle,
    approve_sdk,
    contents,
    wait_turn,
)


async def run(parent: Path, point: str) -> None:
    bundle = ProductBundle([])
    import harnessix.product_config.server as product_server

    async def build(*_args, **_kwargs):
        return bundle

    async def drive(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        thread = await client.create_thread(str(parent / "workspace"), request_id="crash-thread")
        bundle.steps = (_action_step(_proposal()), answer("修改完成"))
        await client.start_turn(thread.thread_id, "修改", request_id="crash-patch")
        completed = await approve_sdk(
            client, thread.thread_id, await wait_turn(client, thread.thread_id, "waiting_approval")
        )
        results = await contents(
            client, thread.thread_id, completed.turn_id, PublicToolResultContent
        )
        original = UUID(results[-1].output["transaction_id"])
        bundle.steps = (rollback_step(original), answer("回滚完成"))
        await client.start_turn(thread.thread_id, "回滚", request_id="crash-rollback")
        waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
        raw = await server.service.runtime.store.get_thread(thread.thread_id)
        from tests.delivery.test_trusted_action_patch import _approval

        inverse = _approval(raw.turns[-1]).plan_id
        (parent / "identities.json").write_text(
            json.dumps(
                {
                    "thread": str(thread.thread_id),
                    "original": str(original),
                    "inverse": str(inverse),
                }
            ),
            encoding="utf-8",
        )

        def exit_at(actual):
            if actual == point:
                os._exit(73)

        filesystem._fault = exit_at
        approvals = await contents(
            client, thread.thread_id, waiting.turn_id, PublicApprovalRequestContent
        )
        request = approvals[-1]
        await client.respond_approval(
            ApprovalRespondParams(
                request_id="crash-approve",
                thread_id=thread.thread_id,
                turn_id=waiting.turn_id,
                approval_id=request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=PublicApprovalDecision(outcome="approved", actor="reviewer"),
            )
        )
        await wait_turn(client, thread.thread_id, "completed")
        raise AssertionError("受控硬退出未命中")

    product_server.build_provider_bundle = build
    product_server.run_stdio = drive
    os.environ["PRIMARY_API_KEY"] = "product-cli-secret-canary"
    os.environ["BACKUP_API_KEY"] = "backup-secret"
    await run_product_stdio(
        config_path=parent / "config.json",
        profile_id=None,
        workspace=parent / "workspace",
        state_directory=parent / "state",
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
