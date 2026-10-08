"""封闭复验版本合同的离线回归；仅使用临时账本与内存Provider。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.evals.cli_config import read_private_eval_config
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import amount_units, format_amount
from harnessix.tools.workspace import digest
from scripts import authorize_provider_reverification as management
from scripts import provider_reverification_plan as plans
from scripts import run_engineering_provider_suite_budgeted as suite_host
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import GuardedVerificationProvider
from tests.evals.test_provider_reverification import plan_for, scoped
from tests.evals.test_provider_reverification_chain import candidate_for
from tests.evals.test_provider_reverification_rebinding import binding_for
from tests.evals.test_provider_verification_budget import (
    PERIOD,
    ObservedProvider,
    bounds,
    consume,
    ledger_file,
    model_request,
    period,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")

V1 = "harnessix.provider-reverification-plan/v1"
V2 = "harnessix.provider-reverification-plan/v2"
KNOWN = "0.725956"
OLD_HOLD = "20.77824"
PRIVATE_MARKER = "private-reverification-v2-fixture-not-for-output"
UNKNOWN_MODES = ("missing", "partial", "alias", "overshoot", "exception", "retry", "after_terminal")

# 升版前实际生成的v1字节与完整Schema摘要，不能从待测类动态生成期望值。
V1_BYTES = (
    b'{"spec_version":"harnessix.provider-reverification-plan/v1",'
    b'"authority":"budget-owner-explicit",'
    b'"reverification_id":"11111111-1111-4111-8111-111111111111",'
    b'"suite_id":"22222222-2222-4222-8222-222222222222",'
    b'"period_id":"f130e2de-e696-4da9-b6c6-2e0aa77e1298",'
    b'"allocation":"70","maximum_cost":"40",'
    b'"ledger_before_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"prior_request_count":2,'
    b'"prior_requests_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    b'"carried_requests":[{"request_id":"33333333-3333-4333-8333-333333333333",'
    b'"reserved_cost":"20.77824",'
    b'"request_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}]}'
)
V1_SCHEMA_SHA256 = "f2e357166711e36c3ab530784ecc9f471e58c50b8c00eef849df1598bd39bd76"


@pytest.fixture(autouse=True)
def isolated_io(tmp_path, monkeypatch):
    """禁用网络、真实模型及凭据入口，并限定Owner只能读取本用例的临时账本。"""

    def forbidden(*args, **kwargs):
        pytest.fail("离线复验回归不得访问网络、真实Provider、Keychain或执行外部命令")

    for target, name in (
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (httpx.Client, "send"),
        (httpx.AsyncClient, "send"),
        (OpenAIChatProvider, "__init__"),
        (suite_host, "_credential"),
        (suite_host, "run_budgeted_suite"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(target, name, forbidden)

    initialize = VerificationBudgetLedger.__init__

    def confined_initialize(self, path, *args, **kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        initialize(self, path, *args, **kwargs)

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", confined_initialize)


def payload(version):
    data = json.loads(V1_BYTES)
    if version == V2:
        data.update(spec_version=V2, allocation="60", maximum_cost="38")
    return data


def model_for(version):
    return (
        plans.VerificationReverificationPlan
        if version == V1
        else plans.VerificationReverificationPlanV2
    )


def held_ledger(tmp_path, *, known=KNOWN, allocation="60"):
    path = ledger_file(tmp_path, allocation=allocation, known=known)
    with VerificationBudgetLedger(path, PERIOD) as owner:
        request = owner.reserve(amount_units(OLD_HOLD), {"purpose": "old-unknown"})
        owner.settle(request, None, sent=True)
    return path


def versioned_plan(path, version=V2):
    data = plan_for(path).model_dump(mode="json")
    if version == V2:
        data.update(spec_version=V2, allocation="60", maximum_cost="38")
    return model_for(version).model_validate_json(json.dumps(data), strict=True)


def authorized_ledger(tmp_path, *, known=KNOWN, version=V2):
    path = held_ledger(tmp_path, known=known, allocation="60" if version == V2 else "70")
    plan = versioned_plan(path, version)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    return path, plan


def active_scope(path, plan, kind):
    if kind == "original":
        return plan
    binding = binding_for(path, plan)
    VerificationBudgetLedger.rebind_reverification(path, binding)
    if kind == "rebound":
        return binding
    candidate = candidate_for(path, plan, binding)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    return candidate


def write_plan(tmp_path, text):
    path = tmp_path / "plan.json"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("schema_mode", ["validation", "serialization"])
def test_v1_serialization_and_complete_schema_are_frozen(schema_mode):
    plan = plans.VerificationReverificationPlan.model_validate_json(V1_BYTES, strict=True)
    assert plan.model_dump_json().encode() == V1_BYTES
    schema = plans.VerificationReverificationPlan.model_json_schema(mode=schema_mode)
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert sha256(encoded).hexdigest() == V1_SCHEMA_SHA256
    parsed = plans.parse_reverification_plan(V1_BYTES.decode())
    assert type(parsed) is plans.VerificationReverificationPlan
    assert parsed.model_dump_json().encode() == V1_BYTES


@pytest.mark.parametrize("version", [V1, V2])
def test_parse_and_private_reader_preserve_exact_version(tmp_path, monkeypatch, version):
    text = json.dumps(payload(version), separators=(",", ":"))
    parsed = plans.parse_reverification_plan(text)
    assert type(parsed) is model_for(version)
    assert parsed.model_dump_json() == text
    shared_reader = plans.read_private_eval_config
    assert shared_reader is read_private_eval_config
    calls = []

    def observed_reader(path, model, **kwargs):
        calls.append((path, kwargs))
        return shared_reader(path, model, **kwargs)

    monkeypatch.setattr(plans, "read_private_eval_config", observed_reader)
    plan_path = write_plan(tmp_path, text)
    restored = plans.read_reverification_plan(str(plan_path))
    assert type(restored) is model_for(version) and restored == parsed
    assert restored.model_dump_json() == text
    assert calls == [(str(plan_path), {"max_bytes": 64 * 1024})]
    assert plan_path.read_text() == text and plan_path.stat().st_mode & 0o777 == 0o600


def test_v2_schema_keeps_literal_sixty_and_thirty_eight():
    schema = plans.VerificationReverificationPlanV2.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(json.loads(V1_BYTES))
    for field, expected in (("spec_version", V2), ("allocation", "60"), ("maximum_cost", "38")):
        assert schema["properties"][field]["const"] == expected
        assert schema["properties"][field]["type"] == "string"


@pytest.mark.parametrize(
    ("version", "allocation", "maximum_cost"),
    [
        (V1, "60", "38"),
        (V1, "60", "40"),
        (V1, "70", "38"),
        (V2, "70", "40"),
        (V2, "70", "38"),
        (V2, "60", "40"),
    ],
)
def test_versions_and_amounts_cannot_be_mixed(version, allocation, maximum_cost):
    data = payload(version)
    data.update(allocation=allocation, maximum_cost=maximum_cost)
    text = json.dumps(data)
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(text)
    with pytest.raises(ValueError):
        model_for(version).model_validate_json(text, strict=True)


@pytest.mark.parametrize("version", [V1, V2])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allocation", 60),
        ("allocation", 70),
        ("allocation", 60.0),
        ("allocation", True),
        ("allocation", "060"),
        ("allocation", "070"),
        ("allocation", "60.0"),
        ("allocation", "70.0"),
        ("allocation", "6e1"),
        ("allocation", " 60"),
        ("maximum_cost", 38),
        ("maximum_cost", 40),
        ("maximum_cost", 38.0),
        ("maximum_cost", False),
        ("maximum_cost", "038"),
        ("maximum_cost", "040"),
        ("maximum_cost", "38.0"),
        ("maximum_cost", "40.0"),
        ("maximum_cost", "3.8e1"),
        ("maximum_cost", "38 "),
        ("prior_request_count", "2"),
        ("prior_request_count", 2.0),
        ("prior_request_count", True),
        ("private_body", PRIVATE_MARKER),
        ("maximumCost", "38"),
    ],
)
def test_numeric_coercion_amount_aliases_and_extra_fields_are_rejected(version, field, value):
    data = payload(version)
    data[field] = value
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(json.dumps(data))


@pytest.mark.parametrize("version", [V1, V2])
@pytest.mark.parametrize("field", ["request_id", "reserved_cost", "request_sha256", "extra"])
def test_carried_request_remains_strict_in_both_versions(version, field):
    data = payload(version)
    data["carried_requests"][0][field] = {
        "request_id": 1,
        "reserved_cost": 20.77824,
        "request_sha256": 1,
        "extra": PRIVATE_MARKER,
    }[field]
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(json.dumps(data))


@pytest.mark.parametrize(
    "version", ["v2", "harnessix.provider-reverification-plan/v02", "v3", None]
)
def test_unknown_or_aliased_version_is_rejected(version):
    data = payload(V2)
    data["spec_version"] = version
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(json.dumps(data))


def test_version_field_alias_cannot_replace_discriminator():
    data = payload(V2)
    data["version"] = data.pop("spec_version")
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(json.dumps(data))


@pytest.mark.parametrize("text", ["null", "[]", "60", '"v2"', "{}", "{", '{"cost":NaN}'])
def test_parser_rejects_non_plan_json(text):
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(text)


@pytest.mark.parametrize("version", [V1, V2])
def test_snapshot_preserves_concrete_version_without_sharing_nested_models(version):
    plan = model_for(version).model_validate_json(json.dumps(payload(version)), strict=True)
    checked = plans.snapshot_reverification_plan(plan)
    assert type(checked) is type(plan) and checked == plan and checked is not plan
    assert checked.carried_requests[0] is not plan.carried_requests[0]
    assert checked.model_dump_json() == plan.model_dump_json()


def forged_plan(plan, method, attack):
    carried = plan.carried_requests[0]
    updates = {
        "allocation_number": {"allocation": int(plan.allocation)},
        "cost_number": {"maximum_cost": int(plan.maximum_cost)},
        "cost_alias": {"maximum_cost": plan.maximum_cost + ".0"},
        "wrong_amount_pair": {
            "allocation": "70" if plan.spec_version == V2 else "60",
            "maximum_cost": "40" if plan.spec_version == V2 else "38",
        },
        "wrong_version": {"spec_version": V1 if plan.spec_version == V2 else V2},
        "uuid_text": {"reverification_id": str(plan.reverification_id)},
        "count_bool": {"prior_request_count": True},
        "count_float": {"prior_request_count": float(plan.prior_request_count)},
        "carried_list": {"carried_requests": list(plan.carried_requests)},
        "carried_dict": {"carried_requests": (dict(carried.__dict__),)},
        "nested_uuid_text": {
            "carried_requests": (
                carried.model_copy(update={"request_id": str(carried.request_id)}),
            )
        },
        "nested_amount_number": {
            "carried_requests": (carried.model_copy(update={"reserved_cost": 20.77824}),)
        },
        "nested_extra": {
            "carried_requests": (carried.model_copy(update={"private_body": PRIVATE_MARKER}),)
        },
        "nested_missing": {
            "carried_requests": (
                plans.CarriedVerificationRequest.model_construct(
                    request_id=carried.request_id, reserved_cost=carried.reserved_cost
                ),
            )
        },
        "extra_field": {},
        "missing_field": {},
    }[attack]
    if method == "copy":
        result = plan.model_copy(update=updates)
    else:
        result = type(plan).model_construct(**{**plan.__dict__, **updates})
    if attack == "extra_field":
        result.__dict__["private_body"] = PRIVATE_MARKER
    elif attack == "missing_field":
        result.__dict__.pop("ledger_before_sha256")
    return result


@pytest.mark.parametrize("version", [V1, V2])
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
    ],
)
def test_snapshot_and_authorize_reject_bypasses_before_ledger_io(
    tmp_path, monkeypatch, version, method, attack
):
    path = held_ledger(tmp_path, allocation="60" if version == V2 else "70")
    bad = forged_plan(versioned_plan(path, version), method, attack)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        plans.snapshot_reverification_plan(bad)

    def forbidden_owner(*args, **kwargs):
        pytest.fail("非法合同必须在打开账本前拒绝")

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", forbidden_owner)
    with pytest.raises(KernelError) as rejected:
        VerificationBudgetLedger.authorize_reverification(path, bad)
    assert rejected.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [None, {}, "v2", 38])
def test_snapshot_rejects_non_contract_objects(value):
    with pytest.raises(ValueError):
        plans.snapshot_reverification_plan(value)


async def test_v2_authorize_idempotence_guard_known_cost_and_reopen_preserve_old_prefix(tmp_path):
    path = held_ledger(tmp_path)
    old = period(path)
    plan = versioned_plan(path)
    assert old["allocation"] == "60" and old["known_cost"] == KNOWN
    assert old["reserved_cost"] == OLD_HOLD and len(old["requests"]) == 2
    VerificationBudgetLedger.authorize_reverification(path, plan)
    registered = path.read_bytes()
    VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == registered
    token = CancelToken()
    with scoped(path, plan) as owner:
        assert type(owner.reverification_plan) is plans.VerificationReverificationPlanV2
        assert owner.reverification_plan == plan and owner.allocation == amount_units("60")
        provider = ObservedProvider(path)
        completions = 0
        guard = GuardedVerificationProvider(provider, owner, bounds(), token)
        async for event in guard.stream(model_request(), CancelToken()):
            if isinstance(event, ResponseCompleted):
                completions += 1
                assert period(path)["requests"][-1]["status"] == "completed"
                assert period(path)["known_cost"] == "0.726024"
    assert completions == 1 and provider.sent == 1 and provider.closed and not token.cancelled
    current = period(path)
    assert current["requests"][: plan.prior_request_count] == old["requests"]
    assert digest(current["requests"][: plan.prior_request_count]) == plan.prior_requests_sha256
    assert current["requests"][-1]["cost_estimate"] == "0.000068"
    assert current["requests"][-1]["reverification_id"] == str(plan.reverification_id)
    assert current["allocation"] == "60" and current["reserved_cost"] == OLD_HOLD
    assert current["bounded_reverification"] == plan.model_dump(mode="json")
    settled = path.read_bytes()
    VerificationBudgetLedger.authorize_reverification(path, plan)
    with scoped(path, plan) as reopened:
        assert reopened.reverification_plan == plan
        assert reopened.period["requests"][: plan.prior_request_count] == old["requests"]
        assert plans.validate_reverification_plan(reopened.period, plan) == amount_units("0.000068")
    assert path.read_bytes() == settled and path.stat().st_mode & 0o777 == 0o600


def test_v2_cumulative_thirty_eight_cap_rejects_one_extra_fixed_point_unit(tmp_path):
    path, plan = authorized_ledger(tmp_path)
    prefix = period(path)["requests"][: plan.prior_request_count]
    with scoped(path, plan) as owner:
        first = owner.reserve(amount_units("19"), {})
        owner.settle(first, amount_units("19"), sent=True)
    with scoped(path, plan) as owner:
        before = path.read_bytes()
        with pytest.raises(KernelError) as rejected:
            owner.reserve(amount_units("19.000000000000000001"), {})
        assert rejected.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        second = owner.reserve(amount_units("19"), {})
        owner.settle(second, amount_units("19"), sent=True)
        before = path.read_bytes()
        with pytest.raises(KernelError) as rejected:
            owner.reserve(1, {})
        assert rejected.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        assert plans.validate_reverification_plan(owner.period, plan) == amount_units("38")
    current = period(path)
    assert current["known_cost"] == "38.725956" and current["reserved_cost"] == OLD_HOLD
    assert current["requests"][: plan.prior_request_count] == prefix
    assert amount_units("60") - amount_units(current["known_cost"]) - amount_units(OLD_HOLD) > 0


@pytest.mark.parametrize(("known", "allowed"), [("1.22176", True), ("1.221760000000000001", False)])
def test_v2_sixty_total_authorization_boundary_does_not_raise_allocation(tmp_path, known, allowed):
    path = held_ledger(tmp_path, known=known)
    plan = versioned_plan(path)
    old = period(path)
    before = path.read_bytes()
    if not allowed:
        with pytest.raises(KernelError) as rejected:
            VerificationBudgetLedger.authorize_reverification(path, plan)
        assert rejected.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before and "bounded_reverification" not in period(path)
        return
    VerificationBudgetLedger.authorize_reverification(path, plan)
    with scoped(path, plan) as owner:
        request = owner.reserve(amount_units("38"), {})
        owner.settle(request, amount_units("38"), sent=True)
    current = period(path)
    assert current["allocation"] == "60" and current["reserved_cost"] == OLD_HOLD
    assert amount_units(current["known_cost"]) + amount_units(
        current["reserved_cost"]
    ) == amount_units("60")
    assert current["requests"][: plan.prior_request_count] == old["requests"]


def test_unscoped_sixty_total_reservation_boundary_rejects_one_extra_unit(tmp_path):
    path = ledger_file(tmp_path, allocation="60", known="59.999999999999999999")
    with VerificationBudgetLedger(path, PERIOD) as owner:
        before = path.read_bytes()
        with pytest.raises(KernelError) as rejected:
            owner.reserve(amount_units("0.000000000000000002"), {})
        assert rejected.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        request = owner.reserve(amount_units("0.000000000000000001"), {})
        owner.settle(request, 1, sent=True)
    assert period(path)["known_cost"] == "60" and period(path)["allocation"] == "60"


@pytest.mark.parametrize("scope_kind", ["original", "rebound", "candidate"])
@pytest.mark.parametrize("mode", UNKNOWN_MODES)
async def test_new_unknown_stops_same_scope_and_reopen_without_releasing_holds(
    tmp_path, scope_kind, mode
):
    path, plan = authorized_ledger(tmp_path)
    identity = active_scope(path, plan, scope_kind)
    prefix = period(path)["requests"][: plan.prior_request_count]
    expected = bounds()
    token = CancelToken()
    with scoped(path, identity) as owner:
        provider = ObservedProvider(path, mode)
        with pytest.raises((KernelError, RuntimeError)):
            await consume(GuardedVerificationProvider(provider, owner, expected, token))
        assert token.cancelled and provider.sent == 1 and provider.closed
        stopped_bytes = path.read_bytes()
        with pytest.raises(KernelError) as rejected:
            owner.reserve(1, {})
        assert rejected.value.code == "verification_budget_unresolved"
        assert path.read_bytes() == stopped_bytes
    current = period(path)
    assert current["requests"][: plan.prior_request_count] == prefix
    assert current["known_cost"] == KNOWN and current["allocation"] == "60"
    assert current["reserved_cost"] == format_amount(
        amount_units(OLD_HOLD) + expected.maximum_units
    )
    assert current["requests"][-1]["status"] == "unknown"
    assert current["requests"][-1]["reserved_cost"] == format_amount(expected.maximum_units)
    assert current["requests"][-1]["reverification_id"] == str(plan.reverification_id)
    assert current["bounded_reverification"] == plan.model_dump(mode="json")
    VerificationBudgetLedger.authorize_reverification(path, plan)
    with pytest.raises(KernelError) as rejected:
        with scoped(path, identity):
            pytest.fail("新unknown不能由原Grant、重绑定或追加候选豁免")
    assert rejected.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == stopped_bytes
    with pytest.raises(KernelError) as rejected:
        with VerificationBudgetLedger(path, PERIOD):
            pytest.fail("默认Scope不得继承旧unknown的特例")
    assert rejected.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == stopped_bytes


@pytest.mark.parametrize(
    "scope",
    ["none", "wrong_grant", "wrong_suite", "missing_suite", "missing_grant", "wrong_period"],
)
def test_wrong_scope_never_inherits_v2_authorization_or_writes(tmp_path, scope):
    path, plan = authorized_ledger(tmp_path)
    grant, suite, period_id = plan.reverification_id, plan.suite_id, PERIOD
    if scope == "none":
        grant = suite = None
    elif scope == "wrong_grant":
        grant = uuid4()
    elif scope == "wrong_suite":
        suite = uuid4()
    elif scope == "missing_suite":
        suite = None
    elif scope == "missing_grant":
        grant = None
    else:
        period_id = uuid4()
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with VerificationBudgetLedger(path, period_id, reverification_id=grant, suite_id=suite):
            pytest.fail("错误Scope不能获得任何预算操作权限")
    assert path.read_bytes() == before


@pytest.mark.parametrize("replacement", ["grant", "suite", "v1"])
def test_v2_grant_cannot_be_replaced_or_downgraded(tmp_path, replacement):
    path, plan = authorized_ledger(tmp_path)
    if replacement == "v1":
        bad = plan_for(path)
    else:
        field = "reverification_id" if replacement == "grant" else "suite_id"
        bad = plan.model_copy(update={field: uuid4()})
    before = path.read_bytes()
    with pytest.raises(KernelError) as rejected:
        VerificationBudgetLedger.authorize_reverification(path, bad)
    assert rejected.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    ["ledger_digest", "prefix_count", "prefix_digest", "old_identity", "old_digest", "old_hold"],
)
def test_v2_registration_rejects_changed_prefix_or_original_hold_without_writing(tmp_path, change):
    path = held_ledger(tmp_path)
    plan = versioned_plan(path)
    carried = plan.carried_requests[0]
    updates = {
        "ledger_digest": {"ledger_before_sha256": "0" * 64},
        "prefix_count": {"prior_request_count": 1},
        "prefix_digest": {"prior_requests_sha256": "0" * 64},
        "old_identity": {"carried_requests": (carried.model_copy(update={"request_id": uuid4()}),)},
        "old_digest": {
            "carried_requests": (carried.model_copy(update={"request_sha256": "0" * 64}),)
        },
        "old_hold": {
            "carried_requests": (
                carried.model_copy(update={"reserved_cost": "20.778240000000000001"}),
            )
        },
    }[change]
    before = path.read_bytes()
    with pytest.raises(KernelError) as rejected:
        VerificationBudgetLedger.authorize_reverification(path, plan.model_copy(update=updates))
    assert rejected.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    ["metadata", "known_cost", "original_hold", "status", "order", "allocation", "cap", "version"],
)
def test_v2_reopen_rejects_persisted_tampering_without_repair(tmp_path, change):
    path, plan = authorized_ledger(tmp_path)
    data = json.loads(path.read_bytes())
    current = data["periods"][0]
    if change == "metadata":
        current["requests"][0]["purpose"] = "modified-prefix"
    elif change == "known_cost":
        current["requests"][0]["cost_estimate"] = "0.725956000000000001"
        current["known_cost"] = "0.725956000000000001"
    elif change == "original_hold":
        current["requests"][-1]["reserved_cost"] = "20.778240000000000001"
        current["reserved_cost"] = "20.778240000000000001"
    elif change == "status":
        current["requests"][-1]["status"] = "reserved"
    elif change == "order":
        current["requests"].reverse()
    elif change == "allocation":
        current["allocation"] = "61"
    elif change == "cap":
        current["bounded_reverification"]["maximum_cost"] = "38.000000000000000001"
    else:
        current["bounded_reverification"]["spec_version"] = V1
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with scoped(path, plan):
            pytest.fail("篡改历史或原预留不得被自动修复")
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [V1, V2])
def test_binding_and_candidate_chain_share_original_version_and_cumulative_cap(tmp_path, version):
    path, plan = authorized_ledger(tmp_path, version=version)
    prefix = period(path)["requests"][: plan.prior_request_count]
    with scoped(path, plan) as owner:
        request = owner.reserve(amount_units("1"), {})
        owner.settle(request, amount_units("1"), sent=True)
    original = binding_for(path, plan)
    assert original.charged_cost == "1"
    VerificationBudgetLedger.rebind_reverification(path, original)
    rebound_bytes = path.read_bytes()
    VerificationBudgetLedger.rebind_reverification(path, original)
    assert path.read_bytes() == rebound_bytes
    with scoped(path, original) as owner:
        request = owner.reserve(amount_units("2"), {})
        owner.settle(request, amount_units("2"), sent=True)
    first = candidate_for(path, plan, original)
    assert first.charged_cost == "3"
    VerificationBudgetLedger.append_reverification_binding(path, first)
    first_bytes = path.read_bytes()
    VerificationBudgetLedger.append_reverification_binding(path, first)
    assert path.read_bytes() == first_bytes
    with scoped(path, first) as owner:
        request = owner.reserve(amount_units("4"), {})
        owner.settle(request, amount_units("4"), sent=True)
    last = candidate_for(path, plan, first, sequence=2)
    assert last.charged_cost == "7"
    assert last.remaining_cost == ("31" if version == V2 else "33")
    VerificationBudgetLedger.append_reverification_binding(path, last)
    for revoked in (plan, original, first):
        before = path.read_bytes()
        with pytest.raises(KernelError) as rejected:
            with scoped(path, revoked):
                pytest.fail("旧Scope不得因合同升版或候选追加重新激活")
        assert rejected.value.code == "verification_budget_unresolved"
        assert path.read_bytes() == before
    with scoped(path, last) as owner:
        assert type(owner.reverification_plan) is model_for(version)
        assert owner.reverification_plan == plan and owner.reverification_binding == original
        assert owner.active_reverification_binding == last
        before = path.read_bytes()
        remaining = amount_units(last.remaining_cost)
        with pytest.raises(KernelError) as rejected:
            owner.reserve(remaining + 1, {})
        assert rejected.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        request = owner.reserve(remaining, {})
        owner.settle(request, remaining, sent=True)
        assert plans.validate_reverification_plan(owner.period, plan) == amount_units(
            plan.maximum_cost
        )
    current = period(path)
    assert current["requests"][: plan.prior_request_count] == prefix
    assert current["bounded_reverification"] == plan.model_dump(mode="json")
    assert current["reverification_binding"] == original.model_dump(mode="json")
    assert current["reverification_binding_chain"] == [
        first.model_dump(mode="json"),
        last.model_dump(mode="json"),
    ]
    assert current["requests"][-1]["suite_id"] == str(last.suite_id)
    assert current["requests"][-1]["reverification_binding_id"] == str(last.binding_id)
    assert current["allocation"] == plan.allocation and current["reserved_cost"] == OLD_HOLD
    assert current["known_cost"] == format_amount(
        amount_units(KNOWN) + amount_units(plan.maximum_cost)
    )
    assert json.loads(path.read_bytes())["schema"] == "harnessix.provider-verification-budget/v3"
    before = path.read_bytes()
    with scoped(path, last) as owner:
        assert owner.active_reverification_binding == last
    assert path.read_bytes() == before


@pytest.mark.parametrize("stage", ["rebind", "candidate"])
@pytest.mark.parametrize("change", ["original_plan_digest", "remaining_forty"])
def test_v2_binding_and_chain_reject_wrong_plan_digest_or_forty_yuan_reset(tmp_path, stage, change):
    path, plan = authorized_ledger(tmp_path)
    with scoped(path, plan) as owner:
        request = owner.reserve(amount_units("1"), {})
        owner.settle(request, amount_units("1"), sent=True)
    original = binding_for(path, plan)
    assert original.charged_cost == "1" and original.remaining_cost == "37"
    if stage == "rebind":
        record = original
        register = VerificationBudgetLedger.rebind_reverification
        active = plan
    else:
        VerificationBudgetLedger.rebind_reverification(path, original)
        with scoped(path, original) as owner:
            request = owner.reserve(amount_units("2"), {})
            owner.settle(request, amount_units("2"), sent=True)
        record = candidate_for(path, plan, original)
        assert record.charged_cost == "3" and record.remaining_cost == "35"
        register = VerificationBudgetLedger.append_reverification_binding
        active = original
    updates = (
        {"original_plan_sha256": "0" * 64}
        if change == "original_plan_digest"
        else {"remaining_cost": "40"}
    )
    before = path.read_bytes()
    with pytest.raises(KernelError) as rejected:
        register(path, record.model_copy(update=updates))
    assert rejected.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before
    with scoped(path, active) as owner:
        assert owner.reverification_plan == plan
        assert owner.period["allocation"] == "60"
        assert owner.period["reserved_cost"] == OLD_HOLD
        assert plans.validate_reverification_plan(owner.period, plan) == amount_units(
            record.charged_cost
        )
    assert path.read_bytes() == before


def invalid_private_plan(tmp_path, text, case):
    if case == "duplicate_top":
        text = text.replace('"allocation":"60"', '"allocation":"60","allocation":"60"', 1)
    elif case == "duplicate_nested":
        text = text.replace(
            '"reserved_cost":"20.77824"',
            '"reserved_cost":"20.77824","reserved_cost":"20.77824"',
            1,
        )
    elif case == "extra_field":
        data = json.loads(text)
        data["private_body"] = PRIVATE_MARKER
        text = json.dumps(data)
    elif case == "unknown_version":
        data = json.loads(text)
        data["spec_version"] = PRIVATE_MARKER
        text = json.dumps(data)
    elif case == "mixed_amounts":
        data = json.loads(text)
        data["maximum_cost"] = "40"
        text = json.dumps(data)
    elif case == "oversized":
        text += " " * (64 * 1024)
    path = write_plan(tmp_path, text)
    if case == "mode0644":
        path.chmod(0o644)
    elif case == "symlink":
        link = tmp_path / "plan-link.json"
        link.symlink_to(path)
        return link, path
    return path, path


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
def test_private_reader_rejects_unsafe_v2_config_without_modifying_it(tmp_path, case):
    text = json.dumps(payload(V2), separators=(",", ":"))
    config, target = invalid_private_plan(tmp_path, text, case)
    before, mode = target.read_bytes(), target.stat().st_mode
    with pytest.raises((ValueError, OSError)):
        plans.read_reverification_plan(str(config))
    assert target.read_bytes() == before and target.stat().st_mode == mode
    if case == "symlink":
        assert config.is_symlink()


@pytest.mark.parametrize("case", ["duplicate_top", "duplicate_nested"])
def test_parser_rejects_duplicate_json_keys_even_with_equal_values(tmp_path, case):
    text = json.dumps(payload(V2), separators=(",", ":"))
    _, target = invalid_private_plan(tmp_path, text, case)
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(target.read_text())


def test_cli_registers_0600_v2_and_repeat_is_byte_idempotent(tmp_path, capsys):
    path = held_ledger(tmp_path)
    old = period(path)
    plan = versioned_plan(path)
    config = write_plan(tmp_path, plan.model_dump_json())
    config_before = config.read_bytes()
    args = ["--plan", str(config), "--budget-ledger", str(path)]
    management.main(args)
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "reason": "authorized",
        "reverification_id": str(plan.reverification_id),
    }
    assert output.err == ""
    registered = path.read_bytes()
    management.main(args)
    assert path.read_bytes() == registered
    assert json.loads(capsys.readouterr().out)["reason"] == "authorized"
    assert config.read_bytes() == config_before
    current = period(path)
    assert current["bounded_reverification"] == plan.model_dump(mode="json")
    assert current["requests"] == old["requests"]
    assert current["allocation"] == "60" and current["known_cost"] == KNOWN
    assert current["reserved_cost"] == OLD_HOLD and path.stat().st_mode & 0o777 == 0o600
    with scoped(path, plan) as owner:
        assert type(owner.reverification_plan) is plans.VerificationReverificationPlanV2


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
def test_cli_rejects_unsafe_v2_before_authorization_without_any_writes(
    tmp_path, monkeypatch, capsys, case
):
    path = held_ledger(tmp_path)
    plan = versioned_plan(path)
    config, target = invalid_private_plan(tmp_path, plan.model_dump_json(), case)
    before, config_before, mode = path.read_bytes(), target.read_bytes(), target.stat().st_mode
    files_before = sorted(item.name for item in tmp_path.iterdir())

    def forbidden_authorization(*args, **kwargs):
        pytest.fail("不安全配置必须在登记或修改账本前拒绝")

    monkeypatch.setattr(
        VerificationBudgetLedger, "authorize_reverification", staticmethod(forbidden_authorization)
    )
    with pytest.raises(SystemExit) as rejected:
        management.main(["--plan", str(config), "--budget-ledger", str(path)])
    assert rejected.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"reason": "verification_reverification_invalid"}
    assert output.err == "" and PRIVATE_MARKER not in output.out + output.err
    assert path.read_bytes() == before and target.read_bytes() == config_before
    assert target.stat().st_mode == mode
    assert sorted(item.name for item in tmp_path.iterdir()) == files_before
    assert not tuple(tmp_path.glob(".budget-*.tmp"))
    if case == "symlink":
        assert config.is_symlink()
