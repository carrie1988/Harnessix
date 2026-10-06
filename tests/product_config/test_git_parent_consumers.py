"""默认认证SDK真实新代际来源与Git基准；原CAS追加不等于Workspace或Git写入。"""

from __future__ import annotations

import io
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_baseline as baseline_module
from harnessix.product_config.action_composition import build_product_action_composition
from harnessix.product_config.git_baseline import collect_product_git_baseline
from harnessix.product_config.git_delivery_source import collect_git_delivery_source
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.server import run_product_stdio
from harnessix.protocol.contracts import PublicToolResultContent
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step
from tests.product_config.conftest import write_config
from tests.product_config.test_git_baseline import command, reader
from tests.product_config.test_git_delivery_source import raises_code
from tests.product_config.test_parent_closure_product_sdk import _proposal, _versions
from tests.product_config.test_product_rollback_sdk import (
    ProductBundle,
    approve_sdk,
    contents,
    wait_turn,
)
from tests.product_config.test_server_and_cli import _credentials


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_default_authenticated_deep_source_baseline_reopen(
    tmp_path, config, monkeypatch, object_format
):
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    proposal = _proposal(root)
    command(root, "init", "-q", "--object-format=" + object_format)
    command(root, "config", "user.name", "Harnessix Test")
    command(root, "config", "user.email", "test@harnessix.invalid")
    command(root, "config", "core.autocrlf", "false")
    command(root, "add", "--", *(item.path for item in proposal.files))
    command(root, "commit", "-qm", "baseline")
    original_head = command(root, "rev-parse", "HEAD")
    path, bundle = write_config(tmp_path / "config.json", config), ProductBundle([])
    observed = {}

    def composition(*args, **kwargs):
        observed["router"] = args[2]
        return build_product_action_composition(*args, **kwargs)

    async def build(*_args, **_kwargs):
        return bundle

    async def project(server):
        thread = await server.service.store.get_thread(observed["thread"])
        router = observed["router"]
        with SQLiteWorkspaceTransactionStore(
            state / "workspace-transactions", read_only=True
        ) as transactions:
            checkpoint = CancelToken().checkpoint
            with raises_code("workspace_closure_unavailable"):
                collect_git_delivery_source(
                    thread, (observed["target"],), router, transactions, checkpoint=checkpoint
                )
            with raises_code("delivery_store_read_only"):
                transactions.put_blob("0" * 64, b"not permitted")
            # 写能力仅由受信宿主显式授予；原SQL Reader仍是只读连接。
            ports = router._snapshot_ports
            changes, routes = transactions._db.total_changes, router._audit.routes()
            index = (root / ".git/index").read_bytes()
            source = collect_git_delivery_source(
                thread,
                (observed["target"],),
                router,
                transactions,
                checkpoint=checkpoint,
                snapshot_ports=ports,
            )
            assert type(source) is ProductGitDeliverySourceV2
            assert len(source.workspace.resources) == 17
            assert (
                len(
                    read_workspace_parent_closure(
                        source.workspace, transactions.blob, checkpoint=checkpoint
                    )
                )
                == 401
            )
            assert (
                ProductGitDeliverySourceV2.model_validate_json(source.model_dump_json()) == source
            )
            baseline = await collect_product_git_baseline(
                thread,
                (observed["target"],),
                router,
                transactions,
                reader(root, state / "git-read"),
                cancel=CancelToken(),
                snapshot_ports=ports,
            )
            assert type(baseline) is ProductGitDeliveryBaselineV2 and baseline.source == source
            assert len(baseline.head_oid) == (40 if object_format == "sha1" else 64)
            assert len(baseline.members) == 16
            assert (
                ProductGitDeliveryBaselineV2.model_validate_json(baseline.model_dump_json())
                == baseline
            )
            assert command(root, "rev-parse", "HEAD") == original_head
            assert (root / ".git/index").read_bytes() == index
            assert transactions._db.total_changes == changes == 0
            assert router._audit.routes() == routes
            assert all((root / item.path).read_bytes() == b"after\n" for item in proposal.files)

            def forbidden(*_args):
                raise AssertionError("未授权目标不得访问CAS")

            with raises_code("git_delivery_source_not_owned"):
                collect_git_delivery_source(
                    thread,
                    (observed["target"], uuid4()),
                    router,
                    transactions,
                    checkpoint=checkpoint,
                    snapshot_ports=WorkspaceSnapshotPorts(forbidden, forbidden),
                )
            # 新最终Native验证仍受整个基准的原60秒控制，不另起读取期限。
            for fault in ("cancel", "deadline"):
                with monkeypatch.context() as context:
                    token, clock = CancelToken(), [0.0]
                    original = baseline_module._verify_final_snapshot

                    context.setattr(
                        baseline_module,
                        "time",
                        SimpleNamespace(monotonic=lambda clock=clock: clock[0]),
                    )

                    def controlled(
                        *args,
                        _fault=fault,
                        _token=token,
                        _clock=clock,
                        _original=original,
                        **kwargs,
                    ):
                        if _fault == "cancel":
                            _token.cancel()
                        else:
                            _clock[0] = 60.0
                        return _original(*args, **kwargs)

                    context.setattr(baseline_module, "_verify_final_snapshot", controlled)
                    expected = (
                        pytest.raises(TurnCancelled)
                        if fault == "cancel"
                        else raises_code("git_baseline_timeout")
                    )
                    with expected:
                        await collect_product_git_baseline(
                            thread,
                            (observed["target"],),
                            router,
                            transactions,
                            reader(root, state / "git-read"),
                            cancel=token,
                            snapshot_ports=ports,
                        )
            return source, baseline

    async def first(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(root), request_id="git-parent-thread")
            observed["thread"] = thread.thread_id
            bundle.steps = (_action_step(proposal), answer("修改完成"))
            await client.start_turn(thread.thread_id, "修改文件", request_id="git-parent-patch")
            waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
            completed = await approve_sdk(client, thread.thread_id, waiting)
            results = await contents(
                client, thread.thread_id, completed.turn_id, PublicToolResultContent
            )
            observed["target"] = UUID(results[-1].output["transaction_id"])
            assert _versions(state, observed["target"])[0].state == "published"
            observed["source"], observed["baseline"] = await project(server)
            assert len(bundle.requests) == 2
        finally:
            await client.close()

    async def reopened(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            assert (await client.get_thread(observed["thread"])).latest_turn.status == "completed"
            source, baseline = await project(server)
            assert source == observed["source"] and baseline == observed["baseline"]
            assert len(bundle.requests) == 2
        finally:
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
    assert not (state / "git-delivery").exists()
