"""正式SDK成功Patch经认证重开派生同一来源，不新增模型、Git或写入入口。"""

from __future__ import annotations

import io
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.action_composition import build_product_action_composition
from harnessix.product_config.git_delivery_source import collect_git_delivery_source
from harnessix.product_config.server import run_product_stdio
from harnessix.protocol.contracts import PublicToolResultContent
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _proposal
from tests.product_config.conftest import write_config
from tests.product_config.test_product_rollback_sdk import (
    ProductBundle,
    approve_sdk,
    contents,
    wait_turn,
)
from tests.product_config.test_server_and_cli import _credentials


async def test_sdk_original_authenticated_source_survives_product_restart(
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
    observed = {}

    def composition(*args, **kwargs):
        actual = build_product_action_composition(*args, **kwargs)
        observed["router"] = args[2]
        return actual

    async def build(*_args, **_kwargs):
        return bundle

    async def project(server):
        # 公共SDK摘要不是来源授权；从默认产品原MAC Reader读取真实Thread。
        thread = await server.service.store.get_thread(observed["thread"])
        with SQLiteWorkspaceTransactionStore(
            state / "workspace-transactions", read_only=True
        ) as transactions:
            before = transactions._db.total_changes
            result = collect_git_delivery_source(
                thread,
                (observed["target"],),
                observed["router"],
                transactions,
                checkpoint=CancelToken().checkpoint,
                snapshot_ports=observed["router"]._snapshot_ports,
            )
            assert transactions._db.total_changes == before
            return result

    async def first(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        thread = await client.create_thread(str(root), request_id="git-source-thread")
        observed["thread"] = thread.thread_id
        bundle.steps = (_action_step(_proposal()), answer("修改完成"))
        await client.start_turn(thread.thread_id, "修改三个文件", request_id="git-source-patch")
        waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
        completed = await approve_sdk(client, thread.thread_id, waiting)
        results = await contents(
            client, thread.thread_id, completed.turn_id, PublicToolResultContent
        )
        observed["target"] = UUID(results[-1].output["transaction_id"])
        observed["source"] = await project(server)
        assert observed["source"].thread_id == thread.thread_id
        assert len(observed["source"].mutations) == 3
        assert len(bundle.requests) == 2
        await client.close()

    async def reopened(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        thread = await client.get_thread(observed["thread"])
        assert thread.latest_turn.status == "completed"
        assert await project(server) == observed["source"]
        assert len(bundle.requests) == 2
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr(
        "harnessix.product_config.action_runtime.build_product_action_composition", composition
    )
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
        if drive is first:
            key = (state / "session-auth/key.v1").read_bytes()
    assert (state / "session-auth/key.v1").read_bytes() == key
    assert not (state / "git-delivery").exists()
