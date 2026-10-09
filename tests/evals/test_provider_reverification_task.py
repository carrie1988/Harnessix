"""BETA-001独立任务授权：只用临时账本，禁止Suite冒用、预算扩张及新增未决豁免。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from harnessix.agent.errors import KernelError
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts import authorize_provider_reverification as management
from scripts import provider_reverification_plan as plans
from scripts import run_engineering_provider_suite_budgeted as suite_host
from scripts.provider_reverification_binding import validate_reverification_binding
from scripts.provider_reverification_chain import validate_candidate_chain
from scripts.provider_verification_budget import VerificationBudgetLedger
from tests.evals.test_provider_reverification import plan_for, scoped
from tests.evals.test_provider_reverification_chain import candidate_for
from tests.evals.test_provider_reverification_rebinding import binding_for
from tests.evals.test_provider_reverification_v2 import (
    KNOWN,
    OLD_HOLD,
    V1,
    V2,
    forged_plan,
    held_ledger,
    invalid_private_plan,
    payload,
    versioned_plan,
    write_plan,
)
from tests.evals.test_provider_verification_budget import PERIOD, period

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")
TASK_VERSION = "harnessix.provider-task-reverification-plan/v1"


@pytest.fixture(autouse=True)
def isolated_io(tmp_path, monkeypatch):
    """网络、凭据与真实Provider均不可用；Owner只能访问本例临时目录。"""

    def forbidden(*args, **kwargs):
        pytest.fail("任务预算测试不得访问网络、真实Provider、凭据或执行外部命令")

    for target, name in (
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (httpx.Client, "send"),
        (httpx.AsyncClient, "send"),
        (OpenAIChatProvider, "__init__"),
        (suite_host, "_credential"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    initialize = VerificationBudgetLedger.__init__

    def confined(self, path, *args, **kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        initialize(self, path, *args, **kwargs)

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", confined)


def task_plan(path):
    data = plan_for(path).model_dump(mode="json")
    del data["suite_id"]
    data.update(spec_version=TASK_VERSION, task_id="BETA-001", allocation="60", maximum_cost="5")
    return plans.parse_reverification_plan(json.dumps(data))


def task_scope(path, plan):
    return VerificationBudgetLedger(
        path, plan.period_id, reverification_id=plan.reverification_id, task_id=plan.task_id
    )


def authorized_task(tmp_path, *, known=KNOWN):
    path = held_ledger(tmp_path, known=known)
    plan = task_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    return path, plan


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_v2_complete_schema_remains_frozen(mode):
    schema = plans.VerificationReverificationPlanV2.model_json_schema(mode=mode)
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    # 在添加task合同前独立取得；v1完整schema摘要由旧回归锁定。
    assert sha256(encoded).hexdigest() == (
        "bcc5ce3a7afa34d7c1ba95e6b6d9c7add33d66e104370933d36940d1876c8945"
    )


def test_task_schema_parser_private_reader_and_snapshot_are_closed(tmp_path):
    plan = task_plan(held_ledger(tmp_path))
    assert type(plan) is plans.VerificationBetaTaskReverificationPlan
    schema = type(plan).model_json_schema()
    assert schema["additionalProperties"] is False
    assert "suite_id" not in schema["properties"]
    assert schema["required"] == list(plan.model_dump())
    for field, literal in (
        ("spec_version", TASK_VERSION),
        ("task_id", "BETA-001"),
        ("allocation", "60"),
        ("maximum_cost", "5"),
    ):
        assert schema["properties"][field]["const"] == literal
        assert schema["properties"][field]["type"] == "string"
    config = write_plan(tmp_path, plan.model_dump_json())
    restored = plans.read_reverification_plan(str(config))
    assert type(restored) is type(plan) and restored == plan
    assert restored.model_dump_json() == plan.model_dump_json()
    snapshot = plans.snapshot_reverification_plan(plan)
    assert snapshot == plan and snapshot is not plan
    assert snapshot.carried_requests[0] is not plan.carried_requests[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allocation", "70"),
        ("allocation", "60.0"),
        ("allocation", 60),
        ("maximum_cost", "38"),
        ("maximum_cost", "40"),
        ("maximum_cost", "5.0"),
        ("maximum_cost", "5.000000000000000001"),
        ("maximum_cost", 5),
        ("task_id", "BETA-002"),
        ("task_id", "BETA-001 "),
        ("task_id", None),
        ("suite_id", None),
        ("suite_id", "22222222-2222-4222-8222-222222222222"),
        ("spec_version", V1),
        ("spec_version", V2),
        ("spec_version", TASK_VERSION + "/unknown"),
        ("reverification_binding_id", None),
    ],
)
def test_task_parser_and_reader_reject_mixed_or_coerced_fields(tmp_path, field, value):
    data = task_plan(held_ledger(tmp_path)).model_dump(mode="json")
    data[field] = value
    text = json.dumps(data)
    config = write_plan(tmp_path, text)
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(text)
    with pytest.raises(ValueError):
        plans.read_reverification_plan(str(config))
    assert config.read_text() == text


@pytest.mark.parametrize("version", [V1, V2])
@pytest.mark.parametrize("task_id", [None, "BETA-001"])
def test_suite_parser_and_reader_reject_task_field_even_null(tmp_path, version, task_id):
    data = payload(version)
    data["task_id"] = task_id
    config = write_plan(tmp_path, json.dumps(data))
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(config.read_text())
    with pytest.raises(ValueError):
        plans.read_reverification_plan(str(config))


@pytest.mark.parametrize(
    "case",
    [
        "mode0644",
        "symlink",
        "duplicate_top",
        "duplicate_nested",
        "extra_field",
        "unknown_version",
        "mixed_amounts",
        "oversized",
    ],
)
def test_task_private_reader_keeps_existing_file_boundary(tmp_path, case):
    text = task_plan(held_ledger(tmp_path)).model_dump_json()
    config, target = invalid_private_plan(tmp_path, text, case)
    before = target.read_bytes()
    with pytest.raises((ValueError, OSError)):
        plans.read_reverification_plan(str(config))
    assert target.read_bytes() == before


@pytest.mark.parametrize("method", ["copy", "construct"])
@pytest.mark.parametrize(
    "attack",
    [
        "allocation_number",
        "cost_number",
        "cost_alias",
        "wrong_amount_pair",
        "wrong_version",
        "uuid_text",
        "count_bool",
        "count_float",
        "carried_list",
        "carried_dict",
        "nested_uuid_text",
        "nested_amount_number",
        "nested_extra",
        "nested_missing",
        "extra_field",
        "missing_field",
        "wrong_task",
        "mixed_scope",
        "missing_task",
    ],
)
def test_task_snapshot_authorization_and_validation_reject_model_bypasses(
    tmp_path, monkeypatch, method, attack
):
    path = held_ledger(tmp_path)
    plan = task_plan(path)
    if attack in {"wrong_task", "mixed_scope", "missing_task"}:
        bad = plan.model_copy() if method == "copy" else type(plan).model_construct(**plan.__dict__)
        if attack == "wrong_task":
            bad.__dict__["task_id"] = "BETA-002"
        elif attack == "mixed_scope":
            bad.__dict__["suite_id"] = uuid4()
        else:
            bad.__dict__.pop("task_id")
    else:
        bad = forged_plan(plan, method, attack)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        plans.snapshot_reverification_plan(bad)
    with pytest.raises(ValueError):
        plans.validate_reverification_plan(period(path), bad)

    def forbidden(*args, **kwargs):
        pytest.fail("伪合同必须在账本IO之前拒绝")

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", forbidden)
    with pytest.raises(KernelError) as rejected:
        VerificationBudgetLedger.authorize_reverification(path, bad)
    assert rejected.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before


def test_task_registration_and_requests_persist_task_identity_and_old_facts(tmp_path):
    path = held_ledger(tmp_path)
    old = period(path)
    plan = task_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    registered = path.read_bytes()
    VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == registered
    current = period(path)
    assert current == {**old, "bounded_reverification": plan.model_dump(mode="json")}
    with task_scope(path, plan) as owner:
        assert owner.suite_id is None and owner.task_id == "BETA-001"
        request = owner.reserve(amount_units("1"), {"purpose": "beta-task"})
        held = period(path)["requests"][-1]
        assert held["task_id"] == "BETA-001"
        assert held["reverification_id"] == str(plan.reverification_id)
        assert held["status"] == "reserved" and held["reserved_cost"] == "1"
        assert "suite_id" not in held and "reverification_binding_id" not in held
        owner.settle(request, amount_units("0.5"), sent=True)
    current = period(path)
    assert current["requests"][: plan.prior_request_count] == old["requests"]
    assert current["reserved_cost"] == OLD_HOLD and current["allocation"] == "60"
    assert amount_units(current["known_cost"]) == amount_units(KNOWN) + amount_units("0.5")
    assert current["requests"][-1]["task_id"] == "BETA-001"
    with task_scope(path, plan) as owner:
        assert plans.validate_reverification_plan(owner.period, plan) == amount_units("0.5")
    assert path.stat().st_mode & 0o777 == 0o600


def test_task_cumulative_five_yuan_cap_includes_reservations_and_survives_reopen(tmp_path):
    path, plan = authorized_task(tmp_path)
    with task_scope(path, plan) as owner:
        request = owner.reserve(amount_units("2"), {})
        owner.settle(request, amount_units("2"), sent=True)
    with task_scope(path, plan) as owner:
        before = path.read_bytes()
        with pytest.raises(KernelError) as exhausted:
            owner.reserve(amount_units("3") + 1, {})
        assert exhausted.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        request = owner.reserve(amount_units("3"), {})
        assert plans.validate_reverification_plan(owner.period, plan) == amount_units("5")
        owner.settle(request, amount_units("3"), sent=True)
    with task_scope(path, plan) as owner:
        with pytest.raises(KernelError) as exhausted:
            owner.reserve(1, {})
        assert exhausted.value.code == "verification_budget_exhausted"
    assert period(path)["reserved_cost"] == OLD_HOLD
    assert period(path)["allocation"] == "60"


@pytest.mark.parametrize(
    ("known", "allowed"), [("34.22176", True), ("34.221760000000000001", False)]
)
def test_task_registration_still_requires_sixty_yuan_total_headroom(tmp_path, known, allowed):
    path = held_ledger(tmp_path, known=known)
    plan = task_plan(path)
    before = path.read_bytes()
    if allowed:
        VerificationBudgetLedger.authorize_reverification(path, plan)
        with task_scope(path, plan) as owner:
            request = owner.reserve(amount_units("5"), {})
            owner.settle(request, amount_units("5"), sent=True)
        assert period(path)["known_cost"] == "39.22176"
    else:
        with pytest.raises(KernelError) as rejected:
            VerificationBudgetLedger.authorize_reverification(path, plan)
        assert rejected.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
    assert period(path)["reserved_cost"] == OLD_HOLD


@pytest.mark.parametrize("status", ["reserved", "unknown"])
def test_new_unresolved_task_request_stops_same_owner_and_reopen_without_refund(tmp_path, status):
    path, plan = authorized_task(tmp_path)
    old = period(path)["requests"]
    with task_scope(path, plan) as owner:
        request = owner.reserve(amount_units("0.55296"), {})
        if status == "unknown":
            owner.settle(request, None, sent=True)
        frozen = path.read_bytes()
        for action in (owner.require_available, lambda: owner.reserve(1, {})):
            with pytest.raises(KernelError) as stopped:
                action()
            assert stopped.value.code == "verification_budget_unresolved"
        assert path.read_bytes() == frozen
    with pytest.raises(KernelError) as stopped:
        with task_scope(path, plan):
            pytest.fail("新增未决不能继承旧unknown豁免")
    assert stopped.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == frozen
    current = period(path)
    assert current["requests"][: plan.prior_request_count] == old
    assert current["requests"][-1]["status"] == status
    assert current["reserved_cost"] == "21.3312"
    # 新计划也不能把本任务的未决吞进一个新的旧前缀。
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_reverification(path, task_plan(path))
    assert path.read_bytes() == frozen


@pytest.mark.parametrize(
    "scope",
    ["none", "missing_grant", "missing_task", "wrong_grant", "wrong_task", "suite", "mixed"],
)
def test_only_matching_task_and_grant_can_open_task_budget(tmp_path, scope):
    path, plan = authorized_task(tmp_path)
    identities = {"reverification_id": plan.reverification_id, "task_id": "BETA-001"}
    if scope == "none":
        identities = {}
    elif scope == "missing_grant":
        identities.pop("reverification_id")
    elif scope == "missing_task":
        identities.pop("task_id")
    elif scope == "wrong_grant":
        identities["reverification_id"] = uuid4()
    elif scope == "wrong_task":
        identities["task_id"] = "BETA-002"
    else:
        identities["suite_id"] = uuid4()
        if scope == "suite":
            identities.pop("task_id")
    before = path.read_bytes()
    with pytest.raises(KernelError) as rejected:
        with VerificationBudgetLedger(path, PERIOD, **identities):
            pytest.fail("Suite或其他身份不得使用task授权")
    assert rejected.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [V1, V2])
def test_task_identity_cannot_use_suite_authorization(tmp_path, version):
    path = held_ledger(tmp_path, allocation="70" if version == V1 else "60")
    plan = versioned_plan(path, version)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    before = path.read_bytes()
    for suite_id in (None, plan.suite_id):
        with pytest.raises(KernelError):
            with VerificationBudgetLedger(
                path,
                PERIOD,
                reverification_id=plan.reverification_id,
                suite_id=suite_id,
                task_id="BETA-001",
            ):
                pytest.fail("任务身份不能混用Suite授权")
    assert path.read_bytes() == before


async def test_actual_suite_entry_rejects_task_authorization_before_credential(
    tmp_path, monkeypatch
):
    path, plan = authorized_task(tmp_path)
    # 只替换Suite配置/镜像前置检查；保留实际run_budgeted_suite及Ledger入口。
    config = SimpleNamespace(suite=SimpleNamespace(plan=SimpleNamespace(suite_id=uuid4())))
    config.model_dump_json = lambda: "{}"
    monkeypatch.setattr(
        suite_host.CodingEvalProviderSuiteRunConfig, "model_validate_json", lambda *a, **k: config
    )
    monkeypatch.setattr(suite_host, "_bounds", lambda *a: None)
    monkeypatch.setattr(suite_host, "_require_scope", lambda *a: None)
    monkeypatch.setattr(suite_host, "_require_images", lambda *a: None)
    before = path.read_bytes()
    with pytest.raises(KernelError) as rejected:
        await suite_host.run_budgeted_suite(
            config,
            budget_path=path,
            period_id=PERIOD,
            allow_network=True,
            reverification_id=plan.reverification_id,
        )
    assert rejected.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["task_id", "suite_id", "reverification_id"])
def test_task_metadata_cannot_forge_authority_even_when_matching(tmp_path, field):
    path, plan = authorized_task(tmp_path)
    with task_scope(path, plan) as owner:
        before = path.read_bytes()
        value = {
            "task_id": "BETA-001",
            "suite_id": None,
            "reverification_id": str(plan.reverification_id),
        }[field]
        with pytest.raises(KernelError) as rejected:
            owner.reserve(1, {field: value})
        assert rejected.value.code == "verification_budget_metadata_invalid"
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "missing_task",
        "wrong_task",
        "suite",
        "null_suite",
        "binding",
        "missing_grant",
        "wrong_grant",
    ],
)
def test_task_suffix_identity_tampering_is_rejected_by_validator_and_reader(tmp_path, change):
    path, plan = authorized_task(tmp_path)
    with task_scope(path, plan) as owner:
        request = owner.reserve(1, {})
        owner.settle(request, 1, sent=True)
    data = json.loads(path.read_bytes())
    current = data["periods"][0]
    request = current["requests"][-1]
    if change == "missing_task":
        request.pop("task_id")
    elif change == "missing_grant":
        request.pop("reverification_id")
    else:
        field, value = {
            "wrong_task": ("task_id", "BETA-002"),
            "suite": ("suite_id", str(uuid4())),
            "null_suite": ("suite_id", None),
            "binding": ("reverification_binding_id", str(uuid4())),
            "wrong_grant": ("reverification_id", str(uuid4())),
        }[change]
        request[field] = value
    with pytest.raises(ValueError):
        plans.validate_reverification_plan(current, plan)
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with task_scope(path, plan):
            pytest.fail("篡改后的任务身份不可恢复请求权限")
    assert path.read_bytes() == before


def test_suite_suffix_cannot_smuggle_task_identity(tmp_path):
    path = held_ledger(tmp_path)
    plan = versioned_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    with scoped(path, plan) as owner:
        request = owner.reserve(1, {})
        owner.settle(request, 1, sent=True)
    current = period(path)
    current["requests"][-1]["task_id"] = "BETA-001"
    with pytest.raises(ValueError):
        plans.validate_reverification_plan(current, plan)


def test_task_authorization_cannot_rebind_or_append_suite_candidate(tmp_path):
    path, plan = authorized_task(tmp_path)
    # 构造字段完整、摘要和额度正确的Suite管理合同，而非仅因缺字段失败。
    suite_plan = versioned_plan(path).model_copy(
        update={"reverification_id": plan.reverification_id, "maximum_cost": "5"}
    )
    binding = binding_for(path, suite_plan).model_copy(
        update={"original_plan_sha256": digest(plan.model_dump(mode="json"))}
    )
    candidate = candidate_for(path, plan, binding)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        validate_reverification_binding(period(path), plan, binding)
    with pytest.raises(ValueError):
        validate_candidate_chain(period(path), plan, binding, (candidate,))
    for action, record in (
        (VerificationBudgetLedger.rebind_reverification, binding),
        (VerificationBudgetLedger.append_reverification_binding, candidate),
    ):
        with pytest.raises(KernelError) as rejected:
            action(path, record)
        assert rejected.value.code == "verification_reverification_invalid"
        assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["reverification_binding", "reverification_binding_chain"])
@pytest.mark.parametrize("value", [None, []])
def test_task_plan_rejects_persisted_suite_binding_fields_even_empty(tmp_path, field, value):
    path, plan = authorized_task(tmp_path)
    data = json.loads(path.read_bytes())
    data["periods"][0][field] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with task_scope(path, plan):
            pytest.fail("任务授权不接受混合绑定字段")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    ["ledger_hash", "prefix_hash", "prefix_count", "carried_hash", "carried_cost", "period"],
)
def test_task_registration_reuses_prefix_and_old_unknown_checks(tmp_path, change):
    path = held_ledger(tmp_path)
    plan = task_plan(path)
    carried = plan.carried_requests[0]
    update = {
        "ledger_hash": {"ledger_before_sha256": "0" * 64},
        "prefix_hash": {"prior_requests_sha256": "0" * 64},
        "prefix_count": {"prior_request_count": plan.prior_request_count + 1},
        "carried_hash": {
            "carried_requests": (carried.model_copy(update={"request_sha256": "0" * 64}),)
        },
        "carried_cost": {"carried_requests": (carried.model_copy(update={"reserved_cost": "1"}),)},
        "period": {"period_id": uuid4()},
    }[change]
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_reverification(path, plan.model_copy(update=update))
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", ["reserved", "completed"])
def test_task_authorization_cannot_adopt_non_unknown_old_request(tmp_path, status):
    path = held_ledger(tmp_path)
    data = json.loads(path.read_bytes())
    current = data["periods"][0]
    old = current["requests"][-1]
    old["status"] = status
    if status == "completed":
        old.update(reserved_cost="0", cost_estimate="0")
        current["reserved_cost"] = "0"
    path.write_text(json.dumps(data))
    plan = task_plan(path)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == before


def test_task_cli_registers_same_contract_without_suite_or_credential_io(tmp_path, capsys):
    path = held_ledger(tmp_path)
    plan = task_plan(path)
    config = write_plan(tmp_path, plan.model_dump_json())
    old = deepcopy(period(path))
    args = ["--plan", str(config), "--budget-ledger", str(path)]
    management.main(args)
    assert json.loads(capsys.readouterr().out) == {
        "reason": "authorized",
        "reverification_id": str(plan.reverification_id),
    }
    before = path.read_bytes()
    management.main(args)
    assert path.read_bytes() == before
    current = period(path)
    assert current == {**old, "bounded_reverification": plan.model_dump(mode="json")}
    with task_scope(path, plan):
        pass
