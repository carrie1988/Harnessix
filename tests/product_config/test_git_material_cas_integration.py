"""完整材料经原 CAS、原批准 Owner 与独立 Git 回读；不证明产品业务闭包。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectRead
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from tests.product_config import test_git_delivery_process as process_tests
from tests.product_config import test_git_material_input as input_tests

make_process = input_tests.make_process


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
@pytest.mark.parametrize("size", [0, 8 * 1024 * 1024], ids=["legal-minimum", "8MiB"])
async def test_complete_cas_material_original_owner_and_independent_readback(
    make_process, tmp_path, object_format, kind, size
) -> None:
    case = make_process(output_redaction=input_tests._Protection())
    binding = input_tests._repository(case, tmp_path, object_format)
    body = input_tests._body(binding, kind, size)
    material = input_tests._material(body, object_format, kind)
    state = case.workspace.parent / "material-cas"
    with SQLiteWorkspaceTransactionStore(state) as store:
        adapter = GitMaterialCAS(store)
        reference = adapter.persist(material)
        serialized = reference.binding()
        # 正文只来自完整 CAS 回读；引用解码不赋予执行权限。
        restored = GitObjectMaterialReference.from_binding(serialized)
        prepared = input_tests._prepare(case, binding, adapter.read(restored))
        write = await process_tests._run(case, prepared)
        assert write.input_proof is not None
        assert write.input_proof.body_bytes == len(body)
        assert write.input_proof.snapshot_sha256 == hashlib.sha256(body).hexdigest()
        process_tests._assert_completion(case, prepared, write, write.stdout, write.stderr)
        # 新 OID 读取建立独立 Plan 和批准，不继承写入批准。
        read = case.port.prepare_object_read(
            case.workspace,
            GitObjectRead(kind, material.object_id, object_format),
            budget=prepared.budget,
        )
        assert read.spec.process_id != prepared.spec.process_id
        observed = await process_tests._run(case, read)
        assert observed.material == material == adapter.read(restored)
        process_tests._assert_completion(case, read, observed, observed.stdout, observed.stderr)
        assert store._db.execute("SELECT count(*) FROM workspace_transactions").fetchone() == (0,)
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as reopened:
        assert GitMaterialCAS(reopened).read(restored) == material
