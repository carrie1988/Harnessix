"""原认证成功 Completion 与诊断接点的实际衔接；不替代原生验收。"""

from __future__ import annotations

from harnessix.agent.cancellation import CancelToken
from harnessix.product_config.git_delivery_process import GitProcessCompletion
from tests.product_config import git_minimum_commit_probe as probe_module
from tests.product_config import test_git_delivery_process as original
from tests.product_config import test_git_material_input as inputs
from tests.product_config import test_git_material_trace2_binding as binding_tests

make_process = inputs.make_process


async def test_success_observation_uses_real_input_proof_without_new_reads(
    make_process, tmp_path, monkeypatch
):
    case, prepared = await binding_tests._prepare(make_process, tmp_path)
    plan = original._plan(case, prepared)
    completion = await case.port.run(
        prepared,
        plan,
        CancelToken(),
        budget=prepared.budget,
        checkpoint=original._checkpoint(plan),
    )
    assert type(completion) is GitProcessCompletion
    original._assert_completion(case, prepared, completion, completion.stdout, completion.stderr)
    assert completion.input_proof is not None
    assert completion.input_proof.producer_pid == completion.lease.pid
    assert not hasattr(completion, "proof")
    probe = probe_module.Probe("A", "stderr-event-v1")
    operation = probe_module.Operation(
        probe, prepared=prepared, finished=True, data={"kind": "write"}
    )
    probe.operations.append(operation)
    seen = []
    project = probe_module.project_operation_trace2

    def observe(op, stderr, git_returncode=None):
        # 仅确认同一原内存引用及回码；不复制原 stderr 正文、不新增取流。
        seen.append(
            (
                op is operation,
                stderr is completion.stderr,
                git_returncode == completion.input_proof.git_returncode,
            )
        )
        return project(op, stderr, git_returncode)

    monkeypatch.setattr(probe_module, "project_operation_trace2", observe)
    probe_module._success(operation, "complete", (), completion)
    assert seen == [(True, True, True)]
    assert operation.data["original_completion_authenticated"] is True
    assert not operation.post_attempted
    observed = operation.data["post_git_trace2"]
    assert observed["reason"] != "RAW_UNAVAILABLE"
    # 原生 Profile 固定在 Windows Git 版本；其他宿主不能被伪造为诊断完整。
    assert probe.incomplete == (observed["completeness"] != "KNOWN")
