"""默认产品认证Root经正式SDK回滚、等待重开与完整状态备份恢复。"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.server import run_product_stdio
from harnessix.product_config.state_backup import backup_product_state, verify_product_backup
from harnessix.product_config.state_restore import restore_product_state
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    ItemPublicEvent,
    PublicApprovalDecision,
    PublicApprovalRequestContent,
    PublicToolResultContent,
)
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _proposal
from tests.product_config.conftest import write_config
from tests.product_config.test_product_patch_rollback import rollback_step
from tests.product_config.test_server_and_cli import _credentials


class ProductBundle(ScriptedProvider):
    """仅替换网络Provider；默认Key、Session、Router、Protocol与执行器真实运行。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


async def wait_turn(client, thread_id, status):
    for _ in range(1000):
        thread = await client.get_thread(thread_id)
        turn = thread.latest_turn
        if turn is not None and turn.status == status:
            return turn
        if turn is not None and turn.status in {"failed", "interrupted"}:
            raise AssertionError(f"默认产品任务失败：{turn.error.code if turn.error else 'none'}")
        await asyncio.sleep(0.01)
    raise AssertionError("默认产品任务未在10秒观察期限内达到目标状态")


async def contents(client, thread_id, turn_id, model):
    replay = await client.replay_events(thread_id, limit=256)
    assert not replay.has_more
    return [
        event.data.item.content
        for event in replay.events
        if event.turn_id == turn_id
        and isinstance(event.data, ItemPublicEvent)
        and isinstance(event.data.item.content, model)
    ]


async def approve_sdk(client, thread_id, turn):
    approvals = await contents(client, thread_id, turn.turn_id, PublicApprovalRequestContent)
    approval = approvals[-1]
    assert approval.diff_artifact is not None and approval.diff_artifact.complete
    await client.respond_approval(
        ApprovalRespondParams(
            request_id=f"approve-{turn.turn_id}",
            thread_id=thread_id,
            turn_id=turn.turn_id,
            approval_id=approval.approval_id,
            fingerprint=approval.request_fingerprint,
            decision=PublicApprovalDecision(outcome="approved", actor="reviewer"),
        )
    )
    return await wait_turn(client, thread_id, "completed")


async def test_default_sdk_rollback_wait_reopen_and_complete_backup_restore(
    tmp_path, config, monkeypatch
):
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/modified.py").write_bytes(b"old\n")
    (root / "tests/deleted.txt").write_bytes(b"remove\n")
    path = write_config(tmp_path / "config.json", config)
    bundle = ProductBundle([])
    identities = {}

    async def build(*_args, **_kwargs):
        return bundle

    async def run(drive):
        monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
        await run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=root,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )

    async def first(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        thread = await client.create_thread(str(root), request_id="create-rollback")
        identities["thread"] = thread.thread_id
        bundle.steps = (_action_step(_proposal()), answer("修改完成"))
        await client.start_turn(thread.thread_id, "修改三个文件", request_id="patch")
        waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
        completed = await approve_sdk(client, thread.thread_id, waiting)
        results = await contents(
            client, thread.thread_id, completed.turn_id, PublicToolResultContent
        )
        identities["original"] = UUID(results[-1].output["transaction_id"])
        bundle.steps = (rollback_step(identities["original"]), answer("回滚完成"))
        await client.start_turn(thread.thread_id, "撤销上次修改", request_id="rollback")
        waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
        identities["rollback-turn"] = waiting.turn_id
        assert len(bundle.requests) == 3
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        await client.close()

    async def second(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        thread = await client.get_thread(identities["thread"])
        assert thread.latest_turn.turn_id == identities["rollback-turn"]
        assert thread.latest_turn.status == "waiting_approval"
        assert len(bundle.requests) == 3  # 重开不重新请求模型或执行回滚。
        completed = await approve_sdk(client, thread.thread_id, thread.latest_turn)
        results = await contents(
            client, thread.thread_id, completed.turn_id, PublicToolResultContent
        )
        assert results[-1].outcome == "succeeded"
        assert results[-1].output["files"] == 3
        identities["inverse"] = UUID(results[-1].output["transaction_id"])
        assert identities["inverse"] != identities["original"]
        assert (root / "src/modified.py").read_bytes() == b"old\n"
        assert not (root / "src/新增.py").exists()
        assert (root / "tests/deleted.txt").read_bytes() == b"remove\n"
        assert len(bundle.requests) == 4
        await client.close()

    async def third(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        thread = await client.get_thread(identities["thread"])
        assert thread.latest_turn.status == "completed"
        results = await contents(
            client, thread.thread_id, thread.latest_turn.turn_id, PublicToolResultContent
        )
        assert results[-1].output["transaction_id"] == str(identities["inverse"])
        assert len(bundle.requests) == 4
        assert (root / "src/modified.py").read_bytes() == b"old\n"
        await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    await run(first)
    key = (state / "session-auth/key.v1").read_bytes()
    await run(second)
    backup = tmp_path / "backup"
    manifest = await backup_product_state(state, backup)
    assert await verify_product_backup(state, backup) == manifest
    restored = await restore_product_state(
        state, backup, restore_id=uuid4(), confirm_backup_id=manifest.backup_id
    )
    assert restored.status == "restored"
    assert (state / "session-auth/key.v1").read_bytes() == key
    await run(third)


@pytest.mark.parametrize("point", ["member_recorded:0", "effect_applied:2"])
async def test_default_product_hard_exit_reconciles_without_replaying_members(
    tmp_path, config, monkeypatch, point
):
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/modified.py").write_bytes(b"old\n")
    (root / "tests/deleted.txt").write_bytes(b"remove\n")
    path = write_config(tmp_path / "config.json", config)
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.product_config.rollback_exit_worker", str(tmp_path), point],
        capture_output=True,
        timeout=45,
    )
    assert process.returncode == 73, process.stderr.decode("utf-8", errors="replace")
    ids = {
        key: UUID(value)
        for key, value in json.loads((tmp_path / "identities.json").read_text()).items()
    }
    before = {
        str(leaf.relative_to(root)): leaf.read_bytes()
        for leaf in (root / "src/modified.py", root / "src/新增.py", root / "tests/deleted.txt")
        if leaf.exists()
    }
    bundle = ProductBundle([])

    async def build(*_args, **_kwargs):
        return bundle

    async def drive(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        await client.get_thread(ids["thread"])
        assert not bundle.requests
        await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    if point == "member_recorded:0":
        with pytest.raises(KernelError) as refused:
            await run_product_stdio(
                config_path=path,
                profile_id=None,
                workspace=root,
                state_directory=state,
                input_stream=io.BytesIO(),
                output_stream=io.BytesIO(),
            )
        assert refused.value.code == "uncertain_effect"
    else:
        await run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=root,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    with SQLiteWorkspaceTransactionStore(state / "workspace-transactions") as transactions:
        assert transactions.load(ids["original"]).state == "published"
        inverse = transactions.load(ids["inverse"])
        assert inverse.state == ("interrupted" if point == "member_recorded:0" else "published")
        assert inverse.cursor == (1 if point == "member_recorded:0" else 3)
    after = {
        str(leaf.relative_to(root)): leaf.read_bytes()
        for leaf in (root / "src/modified.py", root / "src/新增.py", root / "tests/deleted.txt")
        if leaf.exists()
    }
    assert after == before
    assert not bundle.requests
