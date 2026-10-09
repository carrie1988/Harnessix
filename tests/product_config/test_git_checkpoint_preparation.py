"""实际SDK、共享Owner全流读取到冻结Core和正式Review；不执行Git业务写。"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.delivery.git import _GitRunner
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from harnessix.product_config.git_checkpoint_preparation import ProductGitCheckpointPreparer
from harnessix.product_config.git_delivery_process import GitDeliveryProcess
from harnessix.product_config.git_delivery_route_core import load_product_git_delivery_route_core_v2
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.product_config.git_repository_observation import GitRepositoryReadAuthorization
from harnessix.product_config.server import open_default_product_action_runtime
from harnessix.trusted_actions.agent_gateway_invocation import build_agent_action_invocation
from harnessix.trusted_actions.agent_preplanning import plan_agent_action
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_delivery_process import _checkpoint, _plan, _ProcessCase
from tests.support import git_delivery_review as reviews
from tests.support.git_user_observation import run_authenticated_observation


async def _pending(scenario, monkeypatch, owner, inspect, *, before_read=None):
    """只临时注册工具与只读命令计划；实际规划和Review均使用生产实现。"""
    protection = scenario.session._publication._events._protection
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    async with supervisor_type(
        scenario.state / "process-owner", output_redaction=protection
    ) as supervisor:
        host = GitProcessRuntimeHost(owner, supervisor, scenario.router._plans, protection)
        runner = _GitRunner(scenario.reader._executable, scenario.state / "planner-runner")
        port = GitDeliveryProcess(
            runner, scenario.state, output_redaction=protection, runtime_host=host
        )
        case = _ProcessCase(scenario.root, scenario.state, runner, port)
        authorized = []

        async def authorize(prepared):
            if before_read is not None:
                await before_read(prepared)
            # 命令授权仍来自夹具的原正式Plan；不能误认成产品默认策略或Git业务批准。
            plan = _plan(case, prepared)
            approval = _checkpoint(plan)
            authorized.append((prepared, plan, approval))
            return GitRepositoryReadAuthorization(plan, approval)

        parent = scenario.state / "planner-worktrees"
        parent.mkdir(mode=0o700)

        def create(actual, binding, core_store):
            assert actual is scenario
            planner = ProductGitCheckpointPreparer(
                scenario.session,
                scenario.router,
                core_store,
                scenario.reader,
                port,
                parent,
                binding=binding,
                authorize=authorize,
                limits=GitTreeClosureLimits(4096, 64 * 1024 * 1024, 4096, 128),
                max_parents=3,
                snapshot_ports=scenario.router._snapshot_ports,
            )
            planner.error = None
            original = planner.prepare

            async def recorded(*args, **kwargs):
                try:
                    return await original(*args, **kwargs)
                except BaseException as error:
                    planner.error = error
                    raise

            planner.prepare = recorded
            return planner

        async def inspected(pending):
            await inspect(pending, authorized, supervisor)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(reviews, "ActualCorePreparer", create)
                await reviews.pending_actual_review(scenario, patch, inspected, with_provider=True)
        finally:
            await port.aclose()
            assert not supervisor._store.active()


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("continuous", [False, True])
async def test_real_planner_collects_all_objects_preserves_u_and_reuses_original_route(
    tmp_path, config, monkeypatch, fmt, continuous
):
    owners = []

    @asynccontextmanager
    async def captured(*args, **kwargs):
        owners.append(kwargs["root_owner"])
        async with open_default_product_action_runtime(*args, **kwargs) as runtime:
            yield runtime

    monkeypatch.setattr(
        "harnessix.product_config.server.open_default_product_action_runtime", captured
    )

    async def inspect(scenario):
        physical = _source_snapshot(scenario.root)

        async def pending(actual, authorized, supervisor):
            assert authorized
            assert all(
                p.material is not None and p.command.arguments == ("cat-file", "--batch")
                for p, _, _ in authorized
            )
            core = load_product_git_delivery_route_core_v2(
                actual.preparer.core_store, actual.route.plan, checkpoint=lambda: None
            )
            assert len(core.user_observation.baseline.source.patches) == (2 if continuous else 1)
            assert core.delivery_id == actual.route.plan.external_action_id
            assert core.object_scope.roots.base_commit.object_id == core.baseline.head_oid
            assert core.object_scope.roots.base_tree.object_id == core.baseline.head_tree_oid
            assert len(core.baseline.head_oid) == (40 if fmt == "sha1" else 64)
            assert not list(actual.preparer.worktree_parent.iterdir())
            assert core.anchor_intent.parent_identity == core.worktree_intent.parent_identity
            assert _source_snapshot(scenario.root) == physical
            for prepared, plan, approval in authorized:
                handle = supervisor._handles[prepared.spec.process_id]
                assert handle.lease.plan_id == plan.plan_id
                assert handle.lease.plan_fingerprint == plan.fingerprint
                assert approval.plan_fingerprint == plan.fingerprint
                assert scenario.router._plans.load_plan(plan.plan_id) == plan
                assert handle.lease.state == "exited"
                assert handle.lease.returncode == 0
            # 原查询优先路径必须返回相同Core资源，不能重新采集或生成第二组A/D UUID。
            invocation = build_agent_action_invocation(
                actual.thread,
                actual.turn,
                actual.call,
                actual.preparer.binding,
                requires_idempotency=True,
            )
            gateway = (
                scenario.client.transport.server.service.runtime._trusted_actions._state.gateway
            )
            context = gateway._state.context(actual.thread, actual.turn, actual.call)
            n = len(authorized)
            again = await plan_agent_action(
                scenario.router,
                invocation,
                context,
                actual.thread,
                actual.turn,
                actual.call,
                CancelToken(),
            )
            assert again == actual.route
            assert len(authorized) == n
            assert (
                load_product_git_delivery_route_core_v2(
                    actual.preparer.core_store, again.plan, checkpoint=lambda: None
                )
                == core
            )

        await _pending(scenario, monkeypatch, owners[-1], pending)

    await run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect, object_format=fmt, continuous=continuous
    )


@pytest.mark.parametrize("fault", ["source", "deadline", "callback"])
async def test_actual_preparation_failure_never_publishes_partial_review(
    tmp_path, config, monkeypatch, fault
):
    from harnessix.agent.errors import KernelError

    owners = []

    @asynccontextmanager
    async def captured(*args, **kwargs):
        owners.append(kwargs["root_owner"])
        async with open_default_product_action_runtime(*args, **kwargs) as runtime:
            yield runtime

    monkeypatch.setattr(
        "harnessix.product_config.server.open_default_product_action_runtime", captured
    )
    marker = KernelError("checkpoint_test_callback", "合成检查点失败")

    async def inspect(scenario):
        touched = []

        async def faulted(prepared):
            if touched:
                return
            touched.append(True)
            if fault == "source":
                (scenario.root / scenario.selected_paths[0]).write_bytes(b"changed after freeze\n")
            elif fault == "deadline":
                # 测试压缩同一个60秒对象的期限，不改变生产常量或重新分配预算。
                prepared.budget._deadline = 0.0
            else:
                raise marker

        async def unexpected(*args):
            pytest.fail("失败规划不得到达待审批Review")

        before = scenario.router._audit.routes()
        with pytest.raises(KernelError) as caught:
            await _pending(scenario, monkeypatch, owners[-1], unexpected, before_read=faulted)
        if fault == "callback":
            assert caught.value is marker
        elif fault == "deadline":
            assert caught.value.code == "git_process_timeout"
        else:
            # 原正式Plan的完整Workspace复核先拒绝，未启动已变化的对象读取。
            assert caught.value.code == "execution_plan_stale"
        assert scenario.router._audit.routes() == before
        assert not list((scenario.state / "planner-worktrees").iterdir())

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("stop", ["token", "task", "deadline"])
async def test_planner_preserves_injected_owner_settlement_not_outer_cancellation(
    tmp_path, config, monkeypatch, stop
):
    """仅模拟Owner未知结算；真实前置归属和两层托管不冒充原生停止故障验收。"""
    from harnessix.agent.cancellation import CancelToken, TurnCancelled
    from harnessix.agent.errors import KernelError
    from harnessix.product_config import git_checkpoint_preparation as preparation
    from harnessix.product_config import git_delivery_process as processes

    owners, controls = [], {}
    marker = KernelError("git_process_unknown", "合成结算失败")
    marker.reason_group = ("synthetic_receipt_invalid", "synthetic_stop_unverifiable")

    @asynccontextmanager
    async def captured(*args, **kwargs):
        owners.append(kwargs["root_owner"])
        async with open_default_product_action_runtime(*args, **kwargs) as runtime:
            yield runtime

    monkeypatch.setattr(
        "harnessix.product_config.server.open_default_product_action_runtime", captured
    )
    original = preparation._prepare

    async def captured_prepare(*args, **kwargs):
        # cancel 是原第八个参数；尾部新增只读观察端口不改变其归属。
        controls["cancel"], controls["task"] = args[7], asyncio.current_task()
        assert type(controls["cancel"]) is CancelToken
        return await original(*args, **kwargs)

    monkeypatch.setattr(preparation, "_prepare", captured_prepare)

    async def simulated_owner(port, prepared, plan, operation, checkpoint):
        # 不启动进程或伪造回执；该注入只证明原Git结算到实际Planner的异常优先级。
        if stop == "token":
            controls["cancel"].cancel()
        elif stop == "task":
            controls["task"].cancel()
        try:
            await operation.run(asyncio.Event().wait())
        except TurnCancelled:
            raise marker from None

    monkeypatch.setattr(processes, "_execute_process", simulated_owner)

    async def before_read(prepared):
        if stop == "deadline":
            prepared.budget._deadline = time.monotonic() + 0.1

    async def inspect(scenario):
        physical = _source_snapshot(scenario.root)

        async def unexpected(*args):
            pytest.fail("未知结算不得生成审批Review")

        before = scenario.router._audit.routes()
        with pytest.raises(KernelError) as caught:
            await _pending(scenario, monkeypatch, owners[-1], unexpected, before_read=before_read)
        assert caught.value is marker
        assert caught.value.reason_group == (
            "synthetic_receipt_invalid",
            "synthetic_stop_unverifiable",
        )
        assert scenario.router._audit.routes() == before
        assert _source_snapshot(scenario.root) == physical
        assert not list((scenario.state / "planner-worktrees").iterdir())
        # 共享原宿主仍可读取Session，不由准备器关闭或重绑。
        assert (await scenario.session.get_thread(scenario.thread.thread_id)).thread_id == (
            scenario.thread.thread_id
        )

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("role", ["anchor", "delivery"])
@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
async def test_original_intent_becomes_present_after_core_persistence_is_rejected(
    tmp_path, config, monkeypatch, role, kind
):
    """真实Core已持久时外部创建原UUID；末段拒绝而不挑选第二组UUID。"""
    from harnessix.agent.errors import KernelError
    from harnessix.product_config import git_checkpoint_preparation as preparation

    owners, persisted = [], []

    @asynccontextmanager
    async def captured(*args, **kwargs):
        owners.append(kwargs["root_owner"])
        async with open_default_product_action_runtime(*args, **kwargs) as runtime:
            yield runtime

    monkeypatch.setattr(
        "harnessix.product_config.server.open_default_product_action_runtime", captured
    )
    original = preparation._persist_core

    def interference(*args, **kwargs):
        core = original(*args, **kwargs)
        persisted.append(core)
        intent = core.anchor_intent if role == "anchor" else core.worktree_intent
        path = args[0].worktree_parent / str(intent.worktree_id)
        if kind == "file":
            path.write_bytes(b"external fixture interference\n")
        elif kind == "directory":
            path.mkdir()
        else:
            path.symlink_to("missing-external-target", target_is_directory=True)
        return core

    monkeypatch.setattr(preparation, "_persist_core", interference)

    async def inspect(scenario):
        physical = _source_snapshot(scenario.root)

        async def unexpected(*args):
            pytest.fail("失效A/D缺失事实不得到达待审批Review")

        before = scenario.router._audit.routes()
        with pytest.raises(KernelError) as caught:
            await _pending(scenario, monkeypatch, owners[-1], unexpected)
        assert caught.value.code == (
            "workspace_path_denied" if kind == "symlink" else "git_checkpoint_preparation_invalid"
        )
        assert len(persisted) == 1
        assert scenario.router._audit.routes() == before
        assert _source_snapshot(scenario.root) == physical
        assert len(list((scenario.state / "planner-worktrees").iterdir())) == 1

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)
