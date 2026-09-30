"""同一Grant的显式Suite重绑定：原件保留、累计预算、持久化和管理边界。"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import amount_units, format_amount
from harnessix.tools.workspace import digest
from scripts import authorize_provider_reverification as management
from scripts import run_engineering_provider_suite_budgeted as suite_host
from scripts.provider_reverification_binding import VerificationReverificationBinding
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import MODEL, PRICE_SOURCE, GuardedVerificationProvider
from tests.evals.test_provider_reverification import held_ledger, plan_for, scoped
from tests.evals.test_provider_verification_budget import (
    PERIOD,
    ObservedProvider,
    bounds,
    consume,
    ledger_file,
    period,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")

V1 = "harnessix.provider-verification-budget/v1"
V2 = "harnessix.provider-verification-budget/v2"
PRIVATE_MARKER = "private-rebinding-fixture-not-for-output"
BINDING_FIELDS = (
    "spec_version",
    "authority",
    "binding_id",
    "reverification_id",
    "period_id",
    "previous_suite_id",
    "suite_id",
    "ledger_before_sha256",
    "original_plan_sha256",
    "prior_request_count",
    "prior_requests_sha256",
    "charged_cost",
    "remaining_cost",
)


@pytest.fixture(autouse=True)
def isolated_budget_io(tmp_path, monkeypatch):
    """禁止真实Provider、网络和凭据IO；所有Owner必须位于本用例的临时目录。"""

    def forbidden(*args, **kwargs):
        pytest.fail("独立回归不得调用网络、真实Provider或私有凭据")

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
    ):
        monkeypatch.setattr(target, name, forbidden)

    initialize = VerificationBudgetLedger.__init__

    def confined_initialize(self, path, *args, **kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        initialize(self, path, *args, **kwargs)

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", confined_initialize)


def authorized_ledger(tmp_path, charged="0.219136"):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    if amount_units(charged):
        with scoped(path, plan) as owner:
            request = owner.reserve(amount_units(charged), {"purpose": "previous-suite"})
            owner.settle(request, amount_units(charged), sent=True)
    return path, plan


def binding_for(path, plan):
    current = period(path)
    charged = sum(
        amount_units(request["reserved_cost"])
        + (
            amount_units(request["cost_estimate"])
            if request["status"] in {"completed", "not_sent"}
            else 0
        )
        for request in current["requests"][plan.prior_request_count :]
    )
    return VerificationReverificationBinding(
        spec_version="harnessix.provider-reverification-binding/v1",
        authority="budget-owner-explicit",
        binding_id=uuid4(),
        reverification_id=plan.reverification_id,
        period_id=plan.period_id,
        previous_suite_id=plan.suite_id,
        suite_id=uuid4(),
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        original_plan_sha256=digest(plan.model_dump(mode="json")),
        prior_request_count=len(current["requests"]),
        prior_requests_sha256=digest(current["requests"]),
        charged_cost=format_amount(charged),
        remaining_cost=format_amount(amount_units(plan.maximum_cost) - charged),
    )


@pytest.fixture
def authorized(tmp_path):
    return authorized_ledger(tmp_path)


@pytest.fixture
def binding(authorized):
    return binding_for(*authorized)


@pytest.fixture
def rebound(authorized, binding):
    path, plan = authorized
    VerificationBudgetLedger.rebind_reverification(path, binding)
    return path, plan, binding


def new_scope(path, binding):
    return VerificationBudgetLedger(
        path,
        binding.period_id,
        reverification_id=binding.reverification_id,
        suite_id=binding.suite_id,
    )


def write_private_plan(tmp_path, plan):
    path = tmp_path / "private-plan.json"
    path.write_text(plan.model_dump_json())
    path.chmod(0o600)
    return path


def assert_old_facts(path, before):
    current = period(path)
    assert current["requests"][: len(before["requests"])] == before["requests"]
    assert current["bounded_reverification"] == before["bounded_reverification"]
    assert current["allocation"] == before["allocation"] == "70"
    old_unknown = [r for r in before["requests"] if r["status"] == "unknown"]
    assert len(old_unknown) == 1 and old_unknown[0]["reserved_cost"] == "20.77824"
    assert current["reserved_cost"] == "20.77824"


def test_binding_is_strict_frozen_contract_and_json_roundtrips(binding):
    assert isinstance(binding, ContractModel)
    assert set(type(binding).model_fields) == set(BINDING_FIELDS)
    restored = VerificationReverificationBinding.model_validate_json(binding.model_dump_json())
    assert restored == binding
    with pytest.raises(ValidationError):
        binding.suite_id = uuid4()


@pytest.mark.parametrize("field", BINDING_FIELDS)
def test_binding_requires_every_contract_field(binding, field):
    data = binding.model_dump(mode="json")
    del data[field]
    with pytest.raises(ValidationError):
        VerificationReverificationBinding.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("spec_version", "harnessix.provider-reverification-binding/v2"),
        ("authority", "implicit"),
        ("binding_id", "not-a-uuid"),
        ("reverification_id", 1),
        ("period_id", None),
        ("previous_suite_id", "invalid"),
        ("suite_id", "invalid"),
        ("ledger_before_sha256", "0" * 63),
        ("original_plan_sha256", "z" * 64),
        ("prior_requests_sha256", "0" * 65),
        ("prior_request_count", 0),
        ("prior_request_count", 10001),
        ("prior_request_count", True),
        ("prior_request_count", "3"),
        ("prior_request_count", 3.0),
        ("charged_cost", 0.219136),
        ("charged_cost", "-0.1"),
        ("charged_cost", "1e-6"),
        ("charged_cost", "0.0000000000000000001"),
        ("remaining_cost", 39),
        ("remaining_cost", "NaN"),
        ("remaining_cost", " 39.780864"),
        ("private_body", PRIVATE_MARKER),
    ],
)
def test_binding_rejects_extra_fields_and_json_type_coercion(binding, field, value):
    data = binding.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        VerificationReverificationBinding.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("field", ["binding_id", "reverification_id", "period_id", "suite_id"])
def test_binding_python_input_does_not_coerce_uuid_strings(binding, field):
    data = binding.model_dump()
    data[field] = str(data[field])
    with pytest.raises(ValidationError):
        VerificationReverificationBinding.model_validate(data)


@pytest.mark.parametrize("count", [1, 10000])
def test_binding_contract_accepts_request_count_boundaries(binding, count):
    data = binding.model_dump()
    data["prior_request_count"] = count
    assert VerificationReverificationBinding.model_validate(data).prior_request_count == count


@pytest.mark.parametrize("charged", ["0", "0.219136"])
def test_rebind_only_upgrades_schema_and_adds_binding_preserving_all_old_facts(tmp_path, charged):
    path, plan = authorized_ledger(tmp_path, charged)
    binding = binding_for(path, plan)
    before = json.loads(path.read_bytes())
    assert before["schema"] == V1
    VerificationBudgetLedger.rebind_reverification(path, binding)
    expected = deepcopy(before)
    expected["schema"] = V2
    expected["periods"][0]["reverification_binding"] = binding.model_dump(mode="json")
    assert json.loads(path.read_bytes()) == expected
    assert_old_facts(path, before["periods"][0])
    assert binding.charged_cost == charged
    assert binding.remaining_cost == format_amount(amount_units("40") - amount_units(charged))
    assert path.stat().st_mode & 0o777 == 0o600


async def test_rebound_guard_carries_39780864_balance_and_tags_reservation_before_send(
    authorized, binding
):
    path, plan = authorized
    before = period(path)
    assert before["known_cost"] == "0.219204"
    assert binding.charged_cost == "0.219136" and binding.remaining_cost == "39.780864"
    VerificationBudgetLedger.rebind_reverification(path, binding)

    class TaggedProvider(ObservedProvider):
        async def stream(self, request, cancel):
            reserved = period(self.path)["requests"][-1]
            assert reserved["status"] == "reserved"
            assert reserved["reverification_id"] == str(plan.reverification_id)
            assert reserved["reverification_binding_id"] == str(binding.binding_id)
            assert reserved["suite_id"] == str(binding.suite_id)
            async for event in super().stream(request, cancel):
                yield event

    token = CancelToken()
    with new_scope(path, binding) as owner:
        provider = TaggedProvider(path)
        events = await consume(GuardedVerificationProvider(provider, owner, bounds(), token))
    assert isinstance(events[-1], ResponseCompleted)
    assert provider.sent == 1 and provider.closed and not token.cancelled
    assert period(path)["known_cost"] == "0.219272"
    assert period(path)["reverification_binding"] == binding.model_dump(mode="json")
    assert_old_facts(path, before)
    with new_scope(path, binding):
        pass


async def test_same_grant_cumulative_40_boundary_does_not_reset_on_rebind(rebound):
    path, plan, binding = rebound
    before = period(path)
    with new_scope(path, binding) as owner:
        # 使用小于单请求最坏预留的手工额度，单独验证累计40元边界。
        first = owner.reserve(amount_units("19.780864"), {})
        owner.settle(first, amount_units("19.780864"), sent=True)
        exact_before = path.read_bytes()
        with pytest.raises(KernelError):
            owner.reserve(amount_units("20.000000000000000001"), {})
        assert path.read_bytes() == exact_before
        second = owner.reserve(amount_units("20"), {})
        owner.settle(second, amount_units("20"), sent=True)
        exhausted = path.read_bytes()
        with pytest.raises(KernelError):
            owner.reserve(1, {})
        assert path.read_bytes() == exhausted
        token, provider = CancelToken(), ObservedProvider(path)
        with pytest.raises(KernelError):
            await consume(GuardedVerificationProvider(provider, owner, bounds(), token))
        assert token.cancelled and provider.sent == 0 and path.read_bytes() == exhausted
    current = period(path)
    assert current["known_cost"] == "40.000068"
    assert current["reverification_binding"] == binding.model_dump(mode="json")
    assert current["bounded_reverification"]["maximum_cost"] == plan.maximum_cost == "40"
    assert_old_facts(path, before)


@pytest.mark.parametrize("after_request", [False, True])
def test_identical_binding_and_original_grant_remain_bytewise_idempotent(
    rebound, after_request, monkeypatch
):
    path, plan, binding = rebound
    if after_request:
        with new_scope(path, binding) as owner:
            request = owner.reserve(amount_units("0.01"), {})
            owner.settle(request, amount_units("0.001"), sent=True)
    before = path.read_bytes()

    def forbidden_save(self):
        pytest.fail("完全相同的管理计划不得重复发布账本")

    monkeypatch.setattr(VerificationBudgetLedger, "_save", forbidden_save)
    VerificationBudgetLedger.rebind_reverification(path, binding)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == before


@pytest.mark.parametrize("after_request", [False, True])
def test_identical_binding_directory_sync_failure_is_reported_without_rewrite(
    rebound, monkeypatch, after_request
):
    path, _, binding = rebound
    if after_request:
        with new_scope(path, binding) as owner:
            request = owner.reserve(amount_units("0.01"), {})
            owner.settle(request, amount_units("0.001"), sent=True)
    before = path.read_bytes()
    inode = path.stat().st_ino
    calls = []
    sync = os.fsync

    def fail_directory_sync(descriptor):
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        calls.append(descriptor)
        raise OSError(PRIVATE_MARKER)

    def forbidden_save(self):
        pytest.fail("幂等确认只能同步目录，不得再次发布字段")

    monkeypatch.setattr(VerificationBudgetLedger, "_save", forbidden_save)
    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(KernelError) as stopped:
        VerificationBudgetLedger.rebind_reverification(path, binding)
    assert stopped.value.code == "verification_budget_persist_failed"
    assert len(calls) == 1
    assert path.read_bytes() == before and path.stat().st_ino == inode
    monkeypatch.setattr(os, "fsync", sync)
    VerificationBudgetLedger.rebind_reverification(path, binding)
    assert path.read_bytes() == before and path.stat().st_ino == inode


@pytest.mark.parametrize("field", ["binding_id", "suite_id", "ledger_before_sha256"])
def test_second_nonidentical_binding_is_rejected_without_writing(rebound, field):
    path, _, binding = rebound
    replacement = binding.model_copy(update={field: "0" * 64 if "sha256" in field else uuid4()})
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(path, replacement)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "ledger_digest",
        "prefix_count_short",
        "prefix_count_long",
        "prefix_digest",
        "grant_digest",
        "grant_id",
        "charged",
        "remaining",
        "cost_sum_only",
        "previous_suite",
        "same_suite",
        "period",
    ],
)
def test_stale_or_inconsistent_management_binding_is_invalid_and_never_written(
    authorized, binding, change
):
    path, plan = authorized
    updates = {
        "ledger_digest": {"ledger_before_sha256": "0" * 64},
        "prefix_count_short": {"prior_request_count": binding.prior_request_count - 1},
        "prefix_count_long": {"prior_request_count": binding.prior_request_count + 1},
        "prefix_digest": {"prior_requests_sha256": "0" * 64},
        "grant_digest": {"original_plan_sha256": "0" * 64},
        "grant_id": {"reverification_id": uuid4()},
        "charged": {"charged_cost": "0.219135999999999999"},
        "remaining": {"remaining_cost": "39.780864000000000001"},
        "cost_sum_only": {"charged_cost": "0", "remaining_cost": "40"},
        "previous_suite": {"previous_suite_id": uuid4()},
        "same_suite": {"suite_id": plan.suite_id},
        "period": {"period_id": uuid4()},
    }
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(
            path, binding.model_copy(update=updates[change])
        )
    assert path.read_bytes() == before


def test_semantically_identical_but_byte_stale_ledger_is_not_accepted(authorized, binding):
    path, _ = authorized
    path.write_bytes(path.read_bytes() + b"\n")
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(path, binding)
    assert path.read_bytes() == before


def test_binding_does_not_create_a_missing_original_grant(tmp_path):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    binding = binding_for(path, plan)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(path, binding)
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", ["unknown", "reserved"])
def test_new_unresolved_request_cannot_be_adopted_by_management_binding(authorized, status):
    path, plan = authorized
    with scoped(path, plan) as owner:
        request = owner.reserve(amount_units("0.25"), {})
        if status == "unknown":
            owner.settle(request, None, sent=True)
    binding = binding_for(path, plan)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(path, binding)
    assert path.read_bytes() == before
    assert period(path)["requests"][-1]["status"] == status
    assert period(path)["reserved_cost"] == "21.02824"


@pytest.mark.parametrize("sent,cost", [(False, None), (True, 0)])
def test_not_sent_and_known_zero_requests_can_be_frozen_without_charging(authorized, sent, cost):
    path, plan = authorized
    with scoped(path, plan) as owner:
        request = owner.reserve(amount_units("0.25"), {})
        owner.settle(request, cost, sent=sent)
    before = period(path)
    binding = binding_for(path, plan)
    VerificationBudgetLedger.rebind_reverification(path, binding)
    assert binding.charged_cost == "0.219136" and binding.remaining_cost == "39.780864"
    assert_old_facts(path, before)


@pytest.mark.parametrize(
    "scope", ["default", "original_suite", "wrong_suite", "wrong_grant", "grant_only", "suite_only"]
)
async def test_rebound_ledger_never_opens_default_old_or_other_scope_and_never_sends(
    rebound, scope
):
    path, plan, binding = rebound
    grant, suite = binding.reverification_id, binding.suite_id
    if scope == "default":
        grant = suite = None
    elif scope == "original_suite":
        suite = plan.suite_id
    elif scope == "wrong_suite":
        suite = uuid4()
    elif scope == "wrong_grant":
        grant = uuid4()
    elif scope == "grant_only":
        suite = None
    else:
        grant = None
    before = path.read_bytes()
    provider = ObservedProvider(path)
    with pytest.raises(KernelError):
        with VerificationBudgetLedger(
            path, PERIOD, reverification_id=grant, suite_id=suite
        ) as owner:
            await consume(GuardedVerificationProvider(provider, owner, bounds(), CancelToken()))
    assert provider.sent == 0 and path.read_bytes() == before


@pytest.mark.parametrize("mode", ["missing", "partial", "alias", "retry", "after_terminal"])
async def test_rebound_unknown_keeps_old_hold_and_blocks_same_scope_and_restart(rebound, mode):
    path, _, binding = rebound
    before = period(path)
    token, limits = CancelToken(), bounds()
    with new_scope(path, binding) as owner:
        provider = ObservedProvider(path, mode)
        with pytest.raises(KernelError):
            await consume(GuardedVerificationProvider(provider, owner, limits, token))
        assert provider.sent == 1 and provider.closed and token.cancelled
        unknown_body = path.read_bytes()
        with pytest.raises(KernelError):
            owner.reserve(1, {})
        assert path.read_bytes() == unknown_body
    current = period(path)
    assert current["requests"][: len(before["requests"])] == before["requests"]
    assert current["requests"][-1]["status"] == "unknown"
    assert current["requests"][-1]["reserved_cost"] == format_amount(limits.maximum_units)
    expected_held = amount_units("20.77824") + limits.maximum_units
    assert current["reserved_cost"] == format_amount(expected_held)
    with pytest.raises(KernelError):
        with new_scope(path, binding):
            pytest.fail("绑定不能豁免新Suite产生的unknown")
    assert path.read_bytes() == unknown_body


def test_unsettled_rebound_reservation_blocks_restart_without_refund(rebound):
    path, _, binding = rebound
    with new_scope(path, binding) as owner:
        owner.reserve(amount_units("0.01"), {})
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with new_scope(path, binding):
            pytest.fail("持久reserved不能因Owner退出而释放")
    assert path.read_bytes() == before
    assert period(path)["reserved_cost"] == "20.78824"


@pytest.mark.parametrize("field", ["reverification_binding_id", "suite_id", "reverification_id"])
@pytest.mark.parametrize("matching", [False, True])
def test_reservation_metadata_cannot_supply_even_matching_authority_tags(rebound, field, matching):
    path, _, binding = rebound
    value = {
        "reverification_binding_id": binding.binding_id,
        "suite_id": binding.suite_id,
        "reverification_id": binding.reverification_id,
    }[field]
    before = path.read_bytes()
    with new_scope(path, binding) as owner:
        with pytest.raises(KernelError):
            owner.reserve(1, {field: str(value if matching else uuid4())})
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["reverification_binding_id", "suite_id", "reverification_id"])
@pytest.mark.parametrize("change", ["missing", "wrong"])
def test_persisted_new_request_authority_tag_tampering_is_rejected(rebound, field, change):
    path, _, binding = rebound
    with new_scope(path, binding) as owner:
        request = owner.reserve(amount_units("0.01"), {})
        owner.settle(request, amount_units("0.001"), sent=True)
    data = json.loads(path.read_bytes())
    request = data["periods"][0]["requests"][-1]
    if change == "missing":
        del request[field]
    else:
        request[field] = str(uuid4())
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with new_scope(path, binding):
            pytest.fail("持久请求身份篡改不得修复或放行")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change", ["v2_without_binding", "v1_with_binding", "without_grant", "bad_binding_digest"]
)
def test_inconsistent_ledger_version_or_binding_is_rejected_without_repair(rebound, change):
    path, _, binding = rebound
    data = json.loads(path.read_bytes())
    current = data["periods"][0]
    if change == "v2_without_binding":
        del current["reverification_binding"]
    elif change == "v1_with_binding":
        data["schema"] = V1
    elif change == "without_grant":
        del current["bounded_reverification"]
    else:
        current["reverification_binding"]["prior_requests_sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with new_scope(path, binding):
            pytest.fail("账本版本与绑定不一致不得自动修复")
    assert path.read_bytes() == before


@pytest.fixture
def original_v1_owner():
    """从固定Git对象执行未经删改的实际v1 Owner，隔离于当前脚本命名空间。"""
    environment = dict(os.environ)
    source = subprocess.run(
        [
            "git",
            "show",
            "32e974f9f958c0ea7b04ecc60a91919119fab8bf:scripts/provider_verification_budget.py",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    module = ModuleType("harnessix_original_v1_budget_owner_32e974f")
    exec(
        compile(source, "git:32e974f:scripts/provider_verification_budget.py", "exec"),
        module.__dict__,
    )
    return module.VerificationBudgetLedger


def test_actual_frozen_v1_owner_still_reads_original_v1_grant(authorized, original_v1_owner):
    path, plan = authorized
    before = path.read_bytes()
    with original_v1_owner(
        path, PERIOD, reverification_id=plan.reverification_id, suite_id=plan.suite_id
    ) as owner:
        owner.require_available()
        assert owner.period["known_cost"] == "0.219204"
        assert owner.period["reserved_cost"] == "20.77824"
    assert path.read_bytes() == before


@pytest.mark.parametrize("scope", ["default", "original_suite", "new_suite"])
def test_actual_frozen_v1_owner_rejects_v2_in_every_scope_without_repair(
    rebound, original_v1_owner, scope
):
    path, plan, binding = rebound
    grant = plan.reverification_id
    suite = plan.suite_id if scope == "original_suite" else binding.suite_id
    if scope == "default":
        grant = suite = None
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with original_v1_owner(path, PERIOD, reverification_id=grant, suite_id=suite):
            pytest.fail("实际旧Reader必须对v2 fail closed，不能继承原Suite授权")
    assert path.read_bytes() == before
    with new_scope(path, binding):
        pass


@pytest.mark.parametrize("change", ["downgrade", "delete", "replace", "replace_grant"])
def test_running_owner_cannot_publish_downgrade_deleted_or_replaced_binding(rebound, change):
    path, _, binding = rebound
    before = path.read_bytes()
    with new_scope(path, binding) as owner:
        if change == "downgrade":
            owner.data["schema"] = V1
            del owner.period["reverification_binding"]
        elif change == "delete":
            del owner.period["reverification_binding"]
        elif change == "replace":
            owner.period["reverification_binding"]["binding_id"] = str(uuid4())
        else:
            owner.period["bounded_reverification"]["ledger_before_sha256"] = "0" * 64
        # 直接覆盖持久发布边界，避免scope校验提前拒绝掩盖不可降级要求。
        with pytest.raises(KernelError):
            owner._save()
    assert path.read_bytes() == before


def test_binding_management_requires_exclusive_owner_and_releases_lock(authorized, binding):
    path, plan = authorized
    before = path.read_bytes()
    with scoped(path, plan):
        with pytest.raises(KernelError):
            VerificationBudgetLedger.rebind_reverification(path, binding)
        assert path.read_bytes() == before
    VerificationBudgetLedger.rebind_reverification(path, binding)
    with new_scope(path, binding):
        pass


@pytest.mark.parametrize("failure_at", [1, 2])
def test_binding_sync_failure_preserves_unpublished_or_possibly_published_v2_without_io(
    authorized, binding, tmp_path, monkeypatch, failure_at
):
    path, _ = authorized
    before = path.read_bytes()
    old = period(path)
    sync, count = os.fsync, 0

    def fail_sync(descriptor):
        nonlocal count
        count += 1
        if count == failure_at:
            raise OSError(PRIVATE_MARKER)
        sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(path, binding)
    assert count == failure_at
    if failure_at == 1:
        assert path.read_bytes() == before
    else:
        current = json.loads(path.read_bytes())
        assert current["schema"] == V2
        assert current["periods"][0]["reverification_binding"] == binding.model_dump(mode="json")
    assert_old_facts(path, old)
    assert period(path)["known_cost"] == old["known_cost"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert not tuple(tmp_path.glob(".budget-*.tmp"))
    with pytest.raises(KernelError):
        with VerificationBudgetLedger(path, PERIOD):
            pytest.fail("同步失败不授予默认scope")
    monkeypatch.setattr(os, "fsync", sync)
    VerificationBudgetLedger.rebind_reverification(path, binding)
    with new_scope(path, binding):
        pass


def test_external_ledger_change_during_rebind_is_preserved_not_overwritten(
    authorized, binding, monkeypatch
):
    path, _ = authorized
    changed = path.read_bytes() + b"\n"
    sync, calls = os.fsync, 0

    def replace_original(descriptor):
        nonlocal calls
        calls += 1
        sync(descriptor)
        if calls == 1:
            replacement = path.with_suffix(".external")
            replacement.write_bytes(changed)
            replacement.chmod(0o600)
            replacement.replace(path)

    monkeypatch.setattr(os, "fsync", replace_original)
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(path, binding)
    assert path.read_bytes() == changed and json.loads(changed)["schema"] == V1


def test_private_rebind_cli_returns_only_rebound_grant_and_binding_id(
    authorized, binding, tmp_path, capsys
):
    path, _ = authorized
    plan_path = write_private_plan(tmp_path, binding)
    management.main(["--rebind-plan", str(plan_path), "--budget-ledger", str(path)])
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "reason": "rebound",
        "reverification_id": str(binding.reverification_id),
        "binding_id": str(binding.binding_id),
    }
    assert not output.err
    before = path.read_bytes()
    management.main(["--rebind-plan", str(plan_path), "--budget-ledger", str(path)])
    assert json.loads(capsys.readouterr().out)["reason"] == "rebound"
    assert path.read_bytes() == before


@pytest.mark.parametrize("after_rebind", [False, True])
def test_original_private_plan_cli_retains_authorization_and_never_rewrites(
    authorized, binding, tmp_path, capsys, after_rebind
):
    path, plan = authorized
    if after_rebind:
        VerificationBudgetLedger.rebind_reverification(path, binding)
    plan_path = write_private_plan(tmp_path, plan)
    before = path.read_bytes()
    management.main(["--plan", str(plan_path), "--budget-ledger", str(path)])
    assert json.loads(capsys.readouterr().out) == {
        "reason": "authorized",
        "reverification_id": str(plan.reverification_id),
    }
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "case", ["public_mode", "symlink", "extra_field", "malformed", "duplicate_json", "bad_scope"]
)
def test_rebind_cli_rejects_unsafe_private_plan_and_sanitizes_failure(
    authorized, binding, tmp_path, capsys, case
):
    path, _ = authorized
    plan_path = write_private_plan(tmp_path, binding)
    if case == "public_mode":
        plan_path.chmod(0o644)
    elif case == "symlink":
        target = tmp_path / "saved-private-plan.json"
        plan_path.rename(target)
        plan_path.symlink_to(target)
    elif case == "extra_field":
        data = binding.model_dump(mode="json")
        data["private_body"] = PRIVATE_MARKER
        plan_path.write_text(json.dumps(data))
    elif case == "malformed":
        plan_path.write_text("{" + PRIVATE_MARKER)
    elif case == "duplicate_json":
        plan_path.write_text('{"private_body":"' + PRIVATE_MARKER + '","private_body":"x"}')
    else:
        data = binding.model_dump(mode="json")
        data["previous_suite_id"] = str(uuid4())
        plan_path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(SystemExit) as stopped:
        management.main(["--rebind-plan", str(plan_path), "--budget-ledger", str(path)])
    assert stopped.value.code == 1 and path.read_bytes() == before
    output = capsys.readouterr()
    assert json.loads(output.out) == {"reason": "verification_reverification_invalid"}
    assert PRIVATE_MARKER not in output.out + output.err
    assert str(plan_path) not in output.out + output.err and not output.err


@pytest.mark.parametrize("case", ["both", "neither", "unknown_option"])
def test_management_cli_plan_modes_are_mutually_exclusive_and_parser_is_sanitized(
    authorized, binding, tmp_path, capsys, case
):
    path, _ = authorized
    plan_path = write_private_plan(tmp_path, binding)
    args = ["--budget-ledger", str(path)]
    if case == "both":
        args += ["--plan", str(plan_path), "--rebind-plan", str(plan_path)]
    elif case == "unknown_option":
        args += ["--rebind-plan", str(plan_path), "--" + PRIVATE_MARKER]
    before = path.read_bytes()
    with pytest.raises(SystemExit) as stopped:
        management.main(args)
    assert stopped.value.code == 2 and path.read_bytes() == before
    output = capsys.readouterr()
    assert PRIVATE_MARKER not in output.out + output.err
    assert str(plan_path) not in output.out + output.err


@pytest.mark.parametrize("failure_at", [1, 2])
def test_rebind_cli_sync_error_is_sanitized_and_never_reports_success(
    authorized, binding, tmp_path, monkeypatch, capsys, failure_at
):
    path, _ = authorized
    plan_path = write_private_plan(tmp_path, binding)
    old = period(path)
    sync, count = os.fsync, 0

    def fail_sync(descriptor):
        nonlocal count
        count += 1
        if count == failure_at:
            raise OSError(PRIVATE_MARKER)
        sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(SystemExit) as stopped:
        management.main(["--rebind-plan", str(plan_path), "--budget-ledger", str(path)])
    assert stopped.value.code == 1 and count == failure_at
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert set(result) == {"reason"} and result["reason"] not in {"authorized", "rebound"}
    assert PRIVATE_MARKER not in output.out + output.err
    assert not output.err
    assert_old_facts(path, old)
    assert json.loads(path.read_bytes())["schema"] == (V1 if failure_at == 1 else V2)


@pytest.mark.parametrize("after_request", [False, True])
def test_idempotent_rebind_cli_directory_sync_failure_is_sanitized_without_rewrite(
    rebound, tmp_path, monkeypatch, capsys, after_request
):
    path, _, binding = rebound
    if after_request:
        with new_scope(path, binding) as owner:
            request = owner.reserve(amount_units("0.01"), {})
            owner.settle(request, amount_units("0.001"), sent=True)
    plan_path = write_private_plan(tmp_path, binding)
    before = path.read_bytes()
    calls = []

    def fail_directory_sync(descriptor):
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        calls.append(descriptor)
        raise OSError(PRIVATE_MARKER)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(SystemExit) as stopped:
        management.main(["--rebind-plan", str(plan_path), "--budget-ledger", str(path)])
    assert stopped.value.code == 1 and len(calls) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"reason": "verification_budget_persist_failed"}
    assert PRIVATE_MARKER not in output.out + output.err and not output.err
    assert path.read_bytes() == before


@pytest.mark.parametrize("window", ["before_replace", "after_replace", "after_directory_sync"])
def test_real_management_process_exit_recovers_only_identical_binding(
    authorized, binding, tmp_path, monkeypatch, window
):
    """实际子进程退出覆盖发布窗口；确认丢失不重写V2或重新分配额度。"""
    path, _ = authorized
    old = period(path)
    before = path.read_bytes()
    plan_path = write_private_plan(tmp_path, binding)
    code = """
import os, stat, sys
from pathlib import Path
from scripts.provider_reverification_binding import VerificationReverificationBinding
from scripts.provider_verification_budget import VerificationBudgetLedger
plan = VerificationReverificationBinding.model_validate_json(
    Path(sys.argv[2]).read_bytes(), strict=True
)
window = sys.argv[3]
original_replace, original_sync = os.replace, os.fsync
def replace(*args, **kwargs):
    if window == 'before_replace':
        os._exit(23)
    result = original_replace(*args, **kwargs)
    if window == 'after_replace':
        os._exit(23)
    return result
def sync(descriptor):
    original_sync(descriptor)
    if window == 'after_directory_sync' and stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os._exit(23)
os.replace, os.fsync = replace, sync
VerificationBudgetLedger.rebind_reverification(Path(sys.argv[1]), plan)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), str(plan_path), window],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    assert result.returncode == 23
    published = path.read_bytes()
    if window == "before_replace":
        assert published == before and json.loads(published)["schema"] == V1
    else:
        assert json.loads(published)["schema"] == V2

        def no_republication(self):
            pytest.fail("已发布V2的同计划确认不能再次覆盖账本")

        monkeypatch.setattr(VerificationBudgetLedger, "_save", no_republication)
    VerificationBudgetLedger.rebind_reverification(path, binding)
    if window != "before_replace":
        assert path.read_bytes() == published
    assert_old_facts(path, old)
    with new_scope(path, binding):
        pass


def legacy_fingerprint(limits, owner):
    """冻结未绑定v1的既有恢复身份，不以待测fingerprint自身计算预期值。"""
    data = {
        "spec_version": "harnessix.bailian-verification-request-guard/v1",
        "model": MODEL,
        "region": "cn-beijing",
        "mode": "non-thinking",
        "price_source": PRICE_SOURCE,
        "valid_from": limits.valid_from.isoformat(),
        "valid_until": limits.valid_until.isoformat(),
        "max_output_tokens": limits.max_output_tokens,
        "max_attempts": 1,
        "maximum_units": limits.maximum_units,
        "period_id": owner.period_id,
        "ledger_locator_sha256": digest(str(owner.path)),
        "allocation_units": owner.allocation,
    }
    if owner.reverification_plan is not None:
        data["bounded_reverification"] = owner.reverification_plan.model_dump(mode="json")
    return digest(data)


@pytest.mark.parametrize("has_grant", [False, True])
def test_unbound_v1_guard_fingerprint_is_exactly_backward_compatible(tmp_path, has_grant):
    if has_grant:
        path, plan = authorized_ledger(tmp_path)
        ledger = scoped(path, plan)
    else:
        path = ledger_file(tmp_path)
        ledger = VerificationBudgetLedger(path, PERIOD)
    with ledger as owner:
        limits = bounds()
        assert limits.fingerprint(owner) == legacy_fingerprint(limits, owner)


@pytest.mark.parametrize("field", BINDING_FIELDS)
def test_guard_fingerprint_includes_every_binding_field_not_only_id(rebound, field):
    path, _, binding = rebound
    before = path.read_bytes()
    with new_scope(path, binding) as owner:
        limits = bounds()
        original = limits.fingerprint(owner)
        assert original != legacy_fingerprint(limits, owner)
        raw = owner.period["reverification_binding"]
        if field in {"spec_version", "authority"}:
            # Literal值无需制造无效合同；移除整个绑定仍必须改变恢复身份。
            del owner.period["reverification_binding"]
        elif field.endswith("sha256"):
            raw[field] = "0" * 64
        elif field == "prior_request_count":
            raw[field] += 1
        elif field in {"charged_cost", "remaining_cost"}:
            raw[field] = format_amount(amount_units(raw[field]) + 1)
        else:
            raw[field] = str(uuid4())
        assert limits.fingerprint(owner) != original
    assert path.read_bytes() == before
