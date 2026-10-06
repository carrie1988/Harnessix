"""默认产品与认证SDK贯通新代际；离线Provider只替换模型，不替换安全与写入端口。"""

from __future__ import annotations

import hashlib
import io
from uuid import UUID, uuid4

from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.delivery.workspace_v2_contracts import WorkspaceTransactionRecordV2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.execution.versioned_contracts import ExecutionPlanV3
from harnessix.product_config.server import run_product_stdio
from harnessix.product_config.state_backup import backup_product_state, verify_product_backup
from harnessix.product_config.state_restore import restore_product_state
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    PublicApprovalDecision,
    PublicApprovalRequestContent,
    PublicToolResultContent,
)
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step
from tests.product_config.conftest import write_config
from tests.product_config.test_product_patch_rollback import rollback_step
from tests.product_config.test_product_rollback_sdk import (
    ProductBundle,
    approve_sdk,
    contents,
    wait_turn,
)
from tests.product_config.test_server_and_cli import _credentials


def _proposal(root, count=16, depth=24):
    root.mkdir()
    files = []
    for index in range(count):
        path = "/".join([f"d{index:02}", *[f"n{x:02}" for x in range(depth)], "leaf.py"])
        target = root / path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"before\n")
        target.chmod(0o644)
        files.append(
            WorkspacePatchFile(
                operation="replace",
                path=path,
                expected_sha256=hashlib.sha256(b"before\n").hexdigest(),
                content="after\n",
                mode=420,
            )
        )
    return WorkspacePatchInput(files=tuple(files))


def _versions(state, target):
    with (
        SQLiteWorkspaceTransactionStore(
            state / "workspace-transactions", read_only=True
        ) as transactions,
        SQLiteExecutionPlanStore(
            state / "execution-plans.db", read_only=True, read_blob=transactions.blob
        ) as plans,
        SQLiteActionAuditStore(
            state / "action-audit.db", read_only=True, read_blob=transactions.blob
        ) as audit,
    ):
        record = transactions.load(target)
        execution, route = plans.load_plan(target), audit.load(target)
        assert type(record) is WorkspaceTransactionRecordV2
        assert type(execution) is ExecutionPlanV3
        assert type(route) is ActionRouteSnapshotV2
        assert route.plan.execution == execution and record.plan.source == execution.workspace
        history = read_workspace_parent_closure(
            record.plan.source, transactions.blob, checkpoint=lambda: None
        )
        assert len(execution.workspace.resources) == 17 and len(history) == 401
        for (payload,) in transactions._db.execute(
            "SELECT payload FROM workspace_transaction_events WHERE transaction_id=?",
            (str(target),),
        ):
            assert transactions.decode_payload(payload).record.plan == record.plan
        return record, execution, route


async def test_default_authenticated_sdk_patch_rollback_reopen_backup_and_restore(
    tmp_path, config, monkeypatch
):
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    proposal = _proposal(root)
    path = write_config(tmp_path / "config.json", config)
    bundle, observed = ProductBundle([]), {}

    async def build(*_args, **_kwargs):
        return bundle

    async def first(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(root), request_id="closure-thread")
            observed["thread"] = thread.thread_id
            bundle.steps = (_action_step(proposal), answer("修改完成"))
            await client.start_turn(thread.thread_id, "修改文件", request_id="closure-patch")
            waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
            assert all((root / item.path).read_bytes() == b"before\n" for item in proposal.files)
            completed = await approve_sdk(client, thread.thread_id, waiting)
            results = await contents(
                client, thread.thread_id, completed.turn_id, PublicToolResultContent
            )
            assert results[-1].outcome == "succeeded"
            target = UUID(results[-1].output["transaction_id"])
            observed["target"] = target
            assert _versions(state, target)[0].state == "published"
            assert all((root / item.path).read_bytes() == b"after\n" for item in proposal.files)
            bundle.steps = (rollback_step(target), answer("回滚完成"))
            await client.start_turn(thread.thread_id, "撤销修改", request_id="closure-rollback")
            waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
            assert all((root / item.path).read_bytes() == b"after\n" for item in proposal.files)
            completed = await approve_sdk(client, thread.thread_id, waiting)
            results = await contents(
                client, thread.thread_id, completed.turn_id, PublicToolResultContent
            )
            assert results[-1].outcome == "succeeded"
            inverse = UUID(results[-1].output["transaction_id"])
            assert inverse != target
            observed["inverse"] = inverse
            assert _versions(state, inverse)[0].state == "published"
            assert all((root / item.path).read_bytes() == b"before\n" for item in proposal.files)
            observed["requests"] = len(bundle.requests)
        finally:
            await client.close()

    async def reopened(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.get_thread(observed["thread"])
            assert thread.latest_turn.status == "completed"
            assert _versions(state, observed["target"])[0].state == "published"
            assert _versions(state, observed["inverse"])[0].state == "published"
            assert len(bundle.requests) == observed["requests"] == 4
        finally:
            await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    for drive in (first, reopened):
        monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
        await run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=root,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    key = (state / "session-auth/key.v1").read_bytes()
    backup = tmp_path / "backup"
    manifest = await backup_product_state(state, backup)
    assert await verify_product_backup(state, backup) == manifest
    _versions(backup / "state", observed["target"])
    _versions(backup / "state", observed["inverse"])
    restored = await restore_product_state(
        state, backup, restore_id=uuid4(), confirm_backup_id=manifest.backup_id
    )
    assert restored.backup_id == manifest.backup_id
    assert (state / "session-auth/key.v1").read_bytes() == key
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", reopened)
    await run_product_stdio(
        config_path=path,
        profile_id=None,
        workspace=root,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )


async def test_default_sdk_waiting_cancel_preserves_complete_prepared_history(
    tmp_path, config, monkeypatch
):
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    proposal = _proposal(root)
    path, bundle = write_config(tmp_path / "config.json", config), ProductBundle([])

    async def build(*_args, **_kwargs):
        return bundle

    async def drive(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(root), request_id="cancel-thread")
            bundle.steps = (_action_step(proposal),)
            await client.start_turn(thread.thread_id, "修改", request_id="cancel-patch")
            waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
            stopped = await client.cancel_turn(
                thread.thread_id, waiting.turn_id, request_id="cancel-request"
            )
            assert stopped.status == "cancelled"
            assert all((root / item.path).read_bytes() == b"before\n" for item in proposal.files)
            with SQLiteWorkspaceTransactionStore(
                state / "workspace-transactions", read_only=True
            ) as store:
                (target,) = store._db.execute(
                    "SELECT transaction_id FROM workspace_transactions"
                ).fetchone()
            record = _versions(state, UUID(target))[0]
            assert record.state == "prepared" and record.cursor == 0
            assert len(bundle.requests) == 1
        finally:
            await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    await run_product_stdio(
        config_path=path,
        profile_id=None,
        workspace=root,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )


async def test_default_sdk_parent_membership_change_cannot_use_approved_plan(
    tmp_path, config, monkeypatch
):
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    proposal = _proposal(root)
    path, bundle = write_config(tmp_path / "config.json", config), ProductBundle([])

    async def build(*_args, **_kwargs):
        return bundle

    async def drive(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(root), request_id="drift-thread")
            bundle.steps = (_action_step(proposal),)
            await client.start_turn(thread.thread_id, "修改文件", request_id="drift-patch")
            waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
            approvals = await contents(
                client, thread.thread_id, waiting.turn_id, PublicApprovalRequestContent
            )
            approval = approvals[-1]
            (root / "d00/user.txt").write_bytes(b"user-owned\n")
            await client.respond_approval(
                ApprovalRespondParams(
                    request_id="approve-stale",
                    thread_id=thread.thread_id,
                    turn_id=waiting.turn_id,
                    approval_id=approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=PublicApprovalDecision(outcome="approved", actor="reviewer"),
                )
            )
            stopped = await wait_turn(client, thread.thread_id, "interrupted")
            results = await contents(
                client, thread.thread_id, stopped.turn_id, PublicToolResultContent
            )
            assert results[-1].outcome != "succeeded"
            assert all((root / item.path).read_bytes() == b"before\n" for item in proposal.files)
            assert (root / "d00/user.txt").read_bytes() == b"user-owned\n"
            with SQLiteWorkspaceTransactionStore(
                state / "workspace-transactions", read_only=True
            ) as store:
                (target,) = store._db.execute(
                    "SELECT transaction_id FROM workspace_transactions"
                ).fetchone()
            assert _versions(state, UUID(target))[0].state == "prepared"
            assert len(bundle.requests) == 1
        finally:
            await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    await run_product_stdio(
        config_path=path,
        profile_id=None,
        workspace=root,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
