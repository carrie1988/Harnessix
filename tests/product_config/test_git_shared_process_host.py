"""原产品Owner、Supervisor、计划库和冻结保护作用域的真实共享生命周期。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from harnessix.product_config.git_delivery_process import GitDeliveryProcess
from harnessix.product_config.state_owner import product_state_owner
from harnessix.secrets.publication import SecretPublicationScope
from tests.product_config import test_git_delivery_process as original
from tests.product_config import test_git_material_input as inputs

make_process = inputs.make_process
pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要原生Process Owner")


async def test_shared_material_write_and_port_close_keep_original_host_alive(
    make_process, tmp_path: Path
):
    from harnessix.product_config.git_process_host import GitProcessRuntimeHost

    case = make_process()
    repository = inputs._repository(case, tmp_path, "sha256")
    await case.port.aclose()
    protection = SecretPublicationScope((), {})
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    with (
        product_state_owner(case.state) as owner,
        SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
    ):
        async with supervisor_type(
            case.state / "process-owner", output_redaction=protection
        ) as supervisor:
            host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
            case.port = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            material = inputs._material(inputs._body(repository, "commit", 0), "sha256", "commit")
            prepared = inputs._prepare(case, repository, material)
            plan = original._plan(case, prepared)
            result = await case.port.run(
                prepared,
                plan,
                CancelToken(),
                budget=prepared.budget,
                checkpoint=original._checkpoint(plan),
            )
            assert result.input_proof is not None
            original._assert_completion(case, prepared, result, result.stdout, result.stderr)
            assert supervisor._handles[prepared.spec.process_id].lease == result.lease
            assert plans.load_plan(plan.plan_id) == plan
            await case.port.aclose()
            assert not supervisor._closed and not plans._closed
            # 新端口仍借同一个宿主；旧端口关闭不能撤销产品级资源。
            case.port = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            version = case.port.prepare(
                case.workspace, ("version",), budget=original.GitOperationBudget(20)
            )
            observed = await original._run(case, version)
            assert observed.stdout.startswith(b"git version ")
            assert len(supervisor._handles) == 2
    protection.close()


def _rows(state: Path) -> tuple[int, int]:
    """只读观察原两库物理行；不依赖故障后可能已关闭的共享连接。"""
    counts = []
    for relative, table in (
        ("execution-plans.db", "execution_plans"),
        ("process-owner/process-leases.db", "process_leases"),
    ):
        with sqlite3.connect((state / relative).as_uri() + "?mode=ro", uri=True) as db:
            counts.append(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
    return tuple(counts)


@pytest.mark.parametrize(
    "fault",
    [
        "owner",
        "root",
        "plan-root",
        "supervisor-root",
        "closed-plans",
        "closed-supervisor",
        "closed-scope",
        "output-source",
        "host-scope",
        "restore-pending",
        "approval",
    ],
)
async def test_invalid_shared_host_does_not_save_plan_or_start_lease(
    make_process, tmp_path: Path, fault: str
):
    from harnessix.product_config.git_process_host import GitProcessRuntimeHost
    from harnessix.product_config.state_owner import state_owner_anchor

    case = make_process()
    await case.port.aclose()
    protection = SecretPublicationScope((), {})
    other_scope = SecretPublicationScope((), {})
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    with (
        product_state_owner(case.state) as owner,
        SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
    ):
        async with supervisor_type(
            case.state / "process-owner", output_redaction=protection
        ) as supervisor:
            host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
            case.port = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            prepared = case.port.prepare(
                case.workspace, ("version",), budget=original.GitOperationBudget(20)
            )
            plan = original._plan(case, prepared)
            before = _rows(case.state)
            checkpoint = original._checkpoint(plan)
            if fault == "owner":
                owner._active = False
            elif fault == "root":
                case.port._state = case.state / "other"
            elif fault == "plan-root":
                plans._path = case.state / "other.db"
            elif fault == "supervisor-root":
                supervisor._root = case.state / "other-owner"
            elif fault == "closed-plans":
                plans.close()
            elif fault == "closed-supervisor":
                await supervisor.aclose()
            elif fault == "closed-scope":
                protection.close()
            elif fault == "output-source":
                case.port._output_redaction = other_scope
            elif fault == "host-scope":
                case.port._runtime_host = replace(host, protection=other_scope)
            elif fault == "restore-pending":
                (state_owner_anchor(case.state) / "restore-active.json").write_text("{}")
            else:
                checkpoint = None
            with pytest.raises(KernelError):
                await case.port.run(
                    prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=checkpoint
                )
            assert _rows(case.state) == before == (0, 0)
            assert not supervisor._handles
    protection.close()
    other_scope.close()


async def test_shared_call_cancellation_drains_only_its_process(make_process, tmp_path: Path):
    from harnessix.product_config.git_process_host import GitProcessRuntimeHost

    marker = tmp_path / "started"
    program = "import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text('ok');time.sleep(30)"
    case = make_process(python_code=program)
    await case.port.aclose()
    protection = SecretPublicationScope((), {})
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    with (
        product_state_owner(case.state) as owner,
        SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
    ):
        async with supervisor_type(
            case.state / "process-owner", output_redaction=protection
        ) as supervisor:
            host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
            case.port = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            prepared = case.port.prepare(
                case.workspace, (str(marker),), budget=original.GitOperationBudget(20)
            )
            plan = original._plan(case, prepared)
            cancel = CancelToken()
            task = asyncio.create_task(
                case.port.run(
                    prepared,
                    plan,
                    cancel,
                    budget=prepared.budget,
                    checkpoint=original._checkpoint(plan),
                )
            )
            try:
                async with asyncio.timeout(10):
                    for _ in range(500):
                        if await asyncio.to_thread(marker.exists):
                            break
                        await asyncio.sleep(0.02)
                    else:
                        raise AssertionError("实际受监督进程未产生启动见证")
                cancel.cancel()
                with pytest.raises(TurnCancelled):
                    await task
                host.checkpoint(case.state)
                assert not supervisor._closed and not plans._closed
                assert supervisor.status(prepared.spec.process_id).stop_reason == "cancelled"
                assert not supervisor._store.active()
            finally:
                cancel.cancel()
                await asyncio.gather(task, return_exceptions=True)
    protection.close()


async def test_scope_closing_after_real_material_receipt_keeps_effect_unknown(
    make_process, tmp_path: Path, monkeypatch
):
    from harnessix.product_config import git_delivery_process as io
    from harnessix.product_config.git_process_host import GitProcessRuntimeHost

    case = make_process()
    repository = inputs._repository(case, tmp_path, "sha256")
    await case.port.aclose()
    protection = SecretPublicationScope((), {})
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    with (
        product_state_owner(case.state) as owner,
        SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
    ):
        async with supervisor_type(
            case.state / "process-owner", output_redaction=protection
        ) as supervisor:
            host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
            case.port = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            material = inputs._material(inputs._body(repository, "commit", 0), "sha256", "commit")
            prepared = inputs._prepare(case, repository, material)
            plan = original._plan(case, prepared)
            complete = io._complete_process
            reached = []

            async def close_scope(*args, **kwargs):
                result = await complete(*args, **kwargs)
                assert result.input_proof is not None
                reached.append(True)
                protection.close()
                return result

            monkeypatch.setattr(io, "_complete_process", close_scope)
            with pytest.raises(KernelError) as error:
                await case.port.run(
                    prepared,
                    plan,
                    CancelToken(),
                    budget=prepared.budget,
                    checkpoint=original._checkpoint(plan),
                )
            assert error.value.code == "git_material_effect_unknown" and reached == [True]
            assert len(supervisor._handles) == 1
            assert not await asyncio.to_thread(Path(prepared.write.request.body_path).exists)
            assert not supervisor._closed and not plans._closed
    protection.close()
