"""显式诊断模式经原 Plan/Owner 绑定；正常模式与原业务断言保持。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_object_material import GitObjectRead
from harnessix.domain.models import PolicyDecisionKind
from harnessix.product_config.git_delivery_process import GitDeliveryProcess
from tests.product_config import test_git_delivery_process as original
from tests.product_config import test_git_material_input as inputs

make_process = inputs.make_process


async def _prepare(make_process, tmp_path, mode="stderr-event-v1"):
    case = make_process(output_redaction=inputs._Protection())
    binding = inputs._repository(case, tmp_path, "sha256")
    await case.port.aclose()
    case.port = GitDeliveryProcess(
        case.runner, case.state, output_redaction=inputs._Protection(), material_trace2_mode=mode
    )
    material = inputs._material(inputs._body(binding, "commit", 0), "sha256", "commit")
    return case, inputs._prepare(case, binding, material)


async def test_explicit_mode_enters_request_command_spec_plan_before_effect(make_process, tmp_path):
    from harnessix.delivery.git_material_trace2_profile import TRACE2_PROFILE_SHA256

    case, prepared = await _prepare(make_process, tmp_path)
    request = prepared.write.request
    assert len(prepared.command.argv) == len(request.git_argv) == 22
    assert prepared.command.trace2_mode == request.trace2_mode == "stderr-event-v1"
    assert (
        prepared.command.trace2_profile_sha256
        == request.trace2_profile_sha256
        == TRACE2_PROFILE_SHA256
    )
    assert dict(prepared.command.environment)["GIT_TRACE2_EVENT"] == "2"
    assert request.git_environment == prepared.command.environment
    assert request.version == "harnessix.git-material-input/v2"
    assert prepared.spec.input_bytes == len(prepared.write.control_input)
    plan = original._plan(case, prepared)
    assert plan.intent.arguments == prepared.approval_arguments()
    assert plan.policy.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert original._checkpoint(plan).plan_fingerprint == plan.fingerprint
    original._assert_not_started(case)
    normal = case.runner.prepare_command(case.workspace, ("status", "--porcelain"))
    assert normal.trace2_mode == "off"
    assert "GIT_TRACE2_EVENT" not in dict(normal.environment)
    read = case.port.prepare_object_read(
        case.workspace,
        GitObjectRead("commit", request.expected_oid, "sha256"),
        budget=prepared.budget,
    )
    assert read.command.trace2_mode == "off"
    assert "GIT_TRACE2_EVENT" not in dict(read.command.environment)


@pytest.mark.parametrize("policy", [PolicyDecisionKind.ALLOW, PolicyDecisionKind.DENY])
async def test_diagnostic_requires_original_exact_approval_before_stage(
    make_process, tmp_path, policy
):
    case, prepared = await _prepare(make_process, tmp_path)
    plan = original._plan(case, prepared, decision=policy)
    with pytest.raises(KernelError) as error:
        await case.port.run(prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=None)
    assert error.value.code == "approval_required"
    original._assert_not_started(case)


@pytest.mark.parametrize(
    "attack", ["missing", "old-mode", "old-plan", "environment", "bad-profile"]
)
async def test_diagnostic_rejects_changed_binding_before_owner(make_process, tmp_path, attack):
    case, prepared = await _prepare(make_process, tmp_path)
    plan = original._plan(case, prepared)
    checkpoint = original._checkpoint(plan)
    if attack == "missing":
        checkpoint = None
    elif attack == "old-mode":
        case.port._material_trace2_mode = "off"
    elif attack == "old-plan":
        old = case.port.prepare(case.workspace, ("status", "--porcelain"), budget=prepared.budget)
        checkpoint = original._checkpoint(original._plan(case, old))
    elif attack == "environment":
        object.__setattr__(
            prepared.command,
            "environment",
            tuple((k, v) for k, v in prepared.command.environment if k != "GIT_TRACE2_EVENT"),
        )
    elif attack == "bad-profile":
        object.__setattr__(prepared.write.request, "trace2_profile_sha256", "0" * 64)
    with pytest.raises((KernelError, ValueError)):
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=checkpoint
        )
    original._assert_not_started(case)


async def test_normal_off_still_accepts_original_policy_contract(make_process, tmp_path):
    case, prepared = await _prepare(make_process, tmp_path, mode="off")
    assert prepared.write.request.version == "harnessix.git-material-input/v1"
    assert "trace2_mode" not in prepared.write.request.binding()
    plan = original._plan(case, prepared)
    observed = await case.port.run(
        prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=original._checkpoint(plan)
    )
    assert observed.input_proof is not None
    original._assert_completion(case, prepared, observed, observed.stdout, observed.stderr)


async def test_local_explicit_diagnostic_keeps_real_owner_and_strong_proof(make_process, tmp_path):
    case, prepared = await _prepare(make_process, tmp_path)
    plan = original._plan(case, prepared)
    observed = await case.port.run(
        prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=original._checkpoint(plan)
    )
    assert observed.input_proof is not None
    original._assert_completion(case, prepared, observed, observed.stdout, observed.stderr)
    assert b'"event":"version"' in observed.stderr
    assert b'"event":"start"' in observed.stderr
    assert prepared.command.argv == prepared.write.request.git_argv
    assert len(prepared.command.argv) == 22


async def test_command_subclass_cannot_bypass_original_binding_comparison(make_process, tmp_path):
    from harnessix.delivery.git_command import GitCommand

    case, prepared = await _prepare(make_process, tmp_path)

    class Spoof(GitCommand):
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

    fields = {
        name: getattr(prepared.command, name) for name in prepared.command.__dataclass_fields__
    }
    command = Spoof(**fields)
    with pytest.raises(KernelError):
        case.runner.verify_command(command)
    original._assert_not_started(case)


def pytest_addoption(parser) -> None:
    """仅供本文件显式-p本机验证选项接线；默认不加载、不修改原probe。"""
    parser.addoption("--git-material-trace2", default="off", choices=("off", "stderr-event-v1"))


@pytest.mark.parametrize("size", [8 * 1024 * 1024])
async def test_local_explicit_complete_8mib_input_has_same_owner_proof(
    make_process, tmp_path, size
):
    case = make_process(output_redaction=inputs._Protection())
    binding = inputs._repository(case, tmp_path, "sha256")
    await case.port.aclose()
    case.port = GitDeliveryProcess(
        case.runner,
        case.state,
        output_redaction=inputs._Protection(),
        material_trace2_mode="stderr-event-v1",
    )
    body = inputs._body(binding, "commit", size)
    material = inputs._material(body, "sha256", "commit")
    prepared = inputs._prepare(case, binding, material)
    observed = await original._run(case, prepared)
    assert observed.input_proof.body_bytes == size
    assert observed.input_proof.producer_pid == observed.lease.pid
    assert observed.input_proof.snapshot_sha256 == material.body_sha256
    original._assert_completion(case, prepared, observed, observed.stdout, observed.stderr)
    assert not __import__("pathlib").Path(prepared.write.request.body_path).exists()


@pytest.mark.parametrize("attack", ["no-body", "read-purpose", "wrong-accepted", "wrong-protocol"])
async def test_general_binding_cannot_declare_diagnostic_for_other_purposes(
    make_process, tmp_path, attack
):
    from harnessix.delivery.git_material_trace2_profile import TRACE2_PROFILE_SHA256

    case, prepared = await _prepare(make_process, tmp_path)
    arguments = prepared.command.arguments
    options = {
        "input_data": prepared.command.input_data,
        "trace2_mode": "stderr-event-v1",
        "trace2_profile_sha256": TRACE2_PROFILE_SHA256,
    }
    if attack == "no-body":
        options["input_data"] = None
    elif attack == "read-purpose":
        arguments = ("cat-file", "--batch")
    elif attack == "wrong-accepted":
        options["accepted"] = (0, 128)
    elif attack == "wrong-protocol":
        options["allowed_protocols"] = ("https",)
    with pytest.raises(KernelError, match="Git诊断仅允许正式材料写用途"):
        case.runner._binding.prepare(case.workspace, arguments, **options)
    original._assert_not_started(case)


@pytest.mark.parametrize("mode", [None, True, 2, "2", "unknown"])
async def test_host_mode_requires_actual_type_before_preparation(make_process, mode):
    case = make_process(output_redaction=inputs._Protection())
    with pytest.raises(KernelError) as error:
        GitDeliveryProcess(case.runner, case.state, material_trace2_mode=mode)
    assert error.value.code == "git_material_trace2_invalid"
    original._assert_not_started(case)


@pytest.mark.parametrize("field", ["request_fingerprint", "actor"])
async def test_diagnostic_checkpoint_rebuild_rejects_constructed_decision(
    make_process, tmp_path, field
):
    from harnessix.product_config.git_material_process import material_trace2_is_approved

    case, prepared = await _prepare(make_process, tmp_path)
    plan = original._plan(case, prepared)
    checkpoint = original._checkpoint(plan)
    decision = checkpoint.decision.model_copy(
        update={field: "b" * 64 if field == "request_fingerprint" else ""}
    )
    checkpoint = checkpoint.model_copy(update={"decision": decision})
    assert not material_trace2_is_approved(prepared, plan, checkpoint, "stderr-event-v1")
    with pytest.raises(KernelError) as error:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=checkpoint
        )
    assert error.value.code == "approval_required"
    original._assert_not_started(case)
