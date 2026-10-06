"""默认认证SDK的真实Source2准备；只替换网络Provider，不替换安全或Git端口。"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import Thread
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.delivery.workspace_v2_contracts import WorkspaceTransactionRecordV2
from harnessix.execution.contracts import ExecutionPlanV2
from harnessix.execution.versioned_contracts import ExecutionPlanV3
from harnessix.product_config.action_composition import build_product_action_composition
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.server import run_product_stdio
from harnessix.protocol.contracts import PublicToolResultContent
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.contracts import ActionRouteSnapshot
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step
from tests.product_config.conftest import write_config
from tests.product_config.test_git_baseline import command
from tests.product_config.test_parent_closure_product_sdk import _proposal
from tests.product_config.test_product_rollback_sdk import (
    ProductBundle,
    approve_sdk,
    contents,
    wait_turn,
)
from tests.product_config.test_server_and_cli import _credentials
from tests.tools.test_git import _git


@dataclass
class AuthenticatedGitObservation:
    root: Path
    state: Path
    thread: Thread
    targets: tuple[UUID, ...]
    router: TrustedActionRouter
    transactions: SQLiteWorkspaceTransactionStore
    reader: GitReadRuntime
    bundle: ProductBundle
    selected_paths: tuple[str, ...]
    reopened: bool
    session: SQLiteSessionStore
    client: AgentClient

    async def collect(self, *, cancel=None, budget=None, checkpoint=None, **overrides):
        # 延迟导入仅便于独立验证准备流程；缺少实现时正常失败，不跳过API验收。
        from harnessix.product_config.git_user_observation import (
            collect_product_git_user_observation,
        )

        token = cancel or CancelToken()
        kwargs = {
            "cancel": token,
            "budget": budget or GitOperationBudget(60.0),
            "checkpoint": checkpoint or token.checkpoint,
            "snapshot_ports": self.router._snapshot_ports,
            "session": self.session,
            **overrides,
        }
        return await collect_product_git_user_observation(
            self.thread,
            self.targets,
            self.router,
            self.transactions,
            self.reader,
            **kwargs,
        )

    def unchanged_state(self):
        """观察不得改原Session、批准、Route或SQLite事务账本。"""
        return (
            self.thread.model_dump_json(),
            self.router._audit.routes(),
            self.transactions._db.total_changes,
            self.transactions._db.execute(
                "SELECT * FROM workspace_transaction_events ORDER BY transaction_id, sequence"
            ).fetchall(),
            len(self.bundle.requests),
        )


async def run_authenticated_observation(
    tmp_path,
    config,
    monkeypatch,
    inspect,
    *,
    object_format="sha1",
    continuous=False,
    reopen=False,
    deep=False,
    dirty=True,
    legacy=False,
):
    """真实认证Session持久化Patch2后执行检查；重开不重新发起模型请求或批准。"""
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    proposal = _proposal(root, count=16 if deep else 2, depth=24 if deep else 3)
    (root / "user.txt").write_bytes(b"user original\n")
    (root / "unstaged.txt").write_bytes(b"unstaged original\n")
    command(root, "init", "-q", "--object-format=" + object_format)
    command(root, "config", "user.name", "Harnessix Test")
    command(root, "config", "user.email", "test@harnessix.invalid")
    command(root, "config", "core.autocrlf", "false")
    command(root, "add", "--", *(item.path for item in proposal.files), "user.txt", "unstaged.txt")
    command(root, "commit", "-qm", "baseline")
    if dirty:
        (root / "user.txt").write_bytes(b"user staged\n")
        command(root, "add", "--", "user.txt")
        (root / "user.txt").write_bytes(b"user unstaged\n")
        (root / "unstaged.txt").write_bytes(b"unstaged change\n")
        (root / "untracked.txt").write_bytes(b"untracked change\n")

    path, bundle, observed = write_config(tmp_path / "config.json", config), ProductBundle([]), {}

    def composition(*args, **kwargs):
        observed["router"] = args[2]
        return build_product_action_composition(*args, **kwargs)

    async def build(*_args, **_kwargs):
        return bundle

    async def drive(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            is_reopened = "thread" in observed
            if not is_reopened:
                thread = await client.create_thread(str(root), request_id="observation-thread")
                observed["thread"], observed["targets"] = thread.thread_id, []
                original_ports = observed["router"]._snapshot_ports
                if legacy:
                    # 真正沿旧端口生成并认证Patch1；不改写成功结果或补签账本。
                    observed["router"]._snapshot_ports = None
                proposals = [proposal]
                if continuous:
                    proposals.append(
                        WorkspacePatchInput(
                            files=tuple(
                                WorkspacePatchFile(
                                    operation="replace",
                                    path=item.path,
                                    expected_sha256=hashlib.sha256(b"after\n").hexdigest(),
                                    content="final\n",
                                    mode=0o644,
                                )
                                for item in proposal.files
                            )
                        )
                    )
                for index, patch in enumerate(proposals):
                    bundle.steps = (_action_step(patch), answer("修改完成"))
                    await client.start_turn(
                        thread.thread_id, "修改文件", request_id=f"observation-patch-{index}"
                    )
                    waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
                    completed = await approve_sdk(client, thread.thread_id, waiting)
                    results = await contents(
                        client, thread.thread_id, completed.turn_id, PublicToolResultContent
                    )
                    assert results[-1].outcome == "succeeded"
                    observed["targets"].append(UUID(results[-1].output["transaction_id"]))
                observed["router"]._snapshot_ports = original_ports
            else:
                assert (
                    await client.get_thread(observed["thread"])
                ).latest_turn.status == "completed"
            assert len(bundle.requests) == (4 if continuous else 2)
            router = observed["router"]
            session = server.service.store
            assert type(session) is SQLiteSessionStore
            thread = await session.get_thread(observed["thread"])
            transactions = router._snapshot_ports.write_blob.__self__
            assert type(transactions) is SQLiteWorkspaceTransactionStore
            assert not transactions._read_only
            assert router._snapshot_ports.read_blob.__self__ is transactions
            assert (
                session.path.parent
                == transactions._root.parent
                == router._audit._path.parent
                == router._plans._path.parent
                == state
            )
            assert session.path == state / "sessions.db"
            assert transactions._root == state / "workspace-transactions"
            assert router._audit._path == state / "action-audit.db"
            assert router._plans._path == state / "execution-plans.db"
            for target in observed["targets"]:
                record, route = transactions.load(target), router._audit.load(target)
                assert type(record) is (
                    WorkspaceTransactionRecord if legacy else WorkspaceTransactionRecordV2
                )
                assert record.state == "published"
                assert type(route) is (ActionRouteSnapshot if legacy else ActionRouteSnapshotV2)
                assert type(route.plan.execution) is (
                    ExecutionPlanV2 if legacy else ExecutionPlanV3
                )
                assert record.plan.source == route.plan.execution.workspace
            await inspect(
                AuthenticatedGitObservation(
                    root,
                    state,
                    thread,
                    tuple(reversed(observed["targets"])),
                    router,
                    transactions,
                    GitReadRuntime(
                        root,
                        _git(),
                        state_directory=state / "git-read",
                        output_redaction=session._publication._events._protection,
                        for_delivery=True,
                    ),
                    bundle,
                    tuple(item.path for item in proposal.files),
                    is_reopened,
                    session,
                    client,
                )
            )
        finally:
            await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr(
        "harnessix.product_config.action_runtime.build_product_action_composition", composition
    )
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    for _ in range(2 if reopen else 1):
        await run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=root,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    assert not (state / "git-delivery").exists()
