"""同一原产品宿主内完整8MiB材料与独立批准回读，不降低原类型或格式矩阵。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.delivery.git_object_material import GitObjectRead
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from harnessix.product_config.git_delivery_process import GitDeliveryProcess
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.product_config.state_owner import product_state_owner
from harnessix.secrets.publication import SecretPublicationScope
from tests.product_config import test_git_delivery_process as original
from tests.product_config import test_git_material_input as inputs

make_process = inputs.make_process
pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要原生Process Owner")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
async def test_shared_owner_full_eight_mib_write_and_independently_approved_read(
    make_process, tmp_path: Path, object_format: str, kind: str
):
    case = make_process()
    repository = inputs._repository(case, tmp_path, object_format)
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
            material = inputs._material(
                inputs._body(repository, kind, inputs._LIMIT), object_format, kind
            )
            prepared = inputs._prepare(case, repository, material)
            written = await original._run(case, prepared)
            assert written.input_proof is not None
            assert written.input_proof.body_bytes == material.body_bytes == 8 * 1024 * 1024
            assert written.input_proof.snapshot_sha256 == material.body_sha256
            original._assert_completion(case, prepared, written, written.stdout, written.stderr)
            read = case.port.prepare_object_read(
                case.workspace,
                GitObjectRead(kind, material.object_id, object_format),
                budget=prepared.budget,
            )
            returned = await original._run(case, read)
            assert returned.material == material
            assert prepared.spec.process_id != read.spec.process_id
            assert len(supervisor._handles) == 2
            assert plans.load_plan(written.lease.plan_id).plan_id != returned.lease.plan_id
            host.checkpoint(case.state)
            await case.port.aclose()
            assert not supervisor._closed and not plans._closed
    protection.close()
