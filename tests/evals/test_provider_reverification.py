"""原未决全额保留的唯一Suite复验：发送前持久化、双上限、重启和负对照。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts import authorize_provider_reverification as management
from scripts.provider_reverification_plan import (
    CarriedVerificationRequest,
    VerificationReverificationPlan,
)
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import GuardedVerificationProvider
from tests.evals.test_provider_verification_budget import (
    PERIOD,
    ObservedProvider,
    bounds,
    consume,
    ledger_file,
    period,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")


def held_ledger(tmp_path):
    path = ledger_file(tmp_path)
    with VerificationBudgetLedger(path, PERIOD) as owner:
        request_id = owner.reserve(amount_units("20.77824"), {"purpose": "old-unknown"})
        owner.settle(request_id, None, sent=True)
    return path


def plan_for(path, *, suite_id=None):
    current = period(path)
    held = current["requests"][-1]
    return VerificationReverificationPlan(
        spec_version="harnessix.provider-reverification-plan/v1",
        authority="budget-owner-explicit",
        reverification_id=uuid4(),
        suite_id=suite_id or uuid4(),
        period_id=PERIOD,
        allocation="70",
        maximum_cost="40",
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        prior_request_count=len(current["requests"]),
        prior_requests_sha256=digest(current["requests"]),
        carried_requests=(
            CarriedVerificationRequest(
                request_id=UUID(held["request_id"]),
                reserved_cost=held["reserved_cost"],
                request_sha256=digest(held),
            ),
        ),
    )


def scoped(path, plan):
    return VerificationBudgetLedger(
        path,
        PERIOD,
        reverification_id=plan.reverification_id,
        suite_id=plan.suite_id,
    )


async def test_authorized_guard_preserves_entire_prefix_and_settles_only_new_request(tmp_path):
    path = held_ledger(tmp_path)
    old = period(path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    registered = path.read_bytes()
    VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == registered
    token = CancelToken()
    with scoped(path, plan) as owner:
        provider = ObservedProvider(path)
        events = await consume(GuardedVerificationProvider(provider, owner, bounds(), token))
    current = period(path)
    assert isinstance(events[-1], ResponseCompleted) and provider.sent == 1 and not token.cancelled
    assert current["requests"][: len(old["requests"])] == old["requests"]
    assert current["reserved_cost"] == "20.77824" and current["allocation"] == "70"
    assert current["known_cost"] == "0.000136"
    assert current["requests"][-1]["reverification_id"] == str(plan.reverification_id)
    with scoped(path, plan):
        pass
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "mode", ["missing", "partial", "alias", "overshoot", "exception", "retry", "after_terminal"]
)
async def test_any_new_unknown_stops_same_scope_and_restart_without_releasing_old(tmp_path, mode):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    old = period(path)["requests"][: plan.prior_request_count]
    token = CancelToken()
    with scoped(path, plan) as owner:
        provider = ObservedProvider(path, mode)
        with pytest.raises((KernelError, RuntimeError)):
            await consume(GuardedVerificationProvider(provider, owner, bounds(), token))
        assert token.cancelled and provider.sent == 1 and provider.closed
        with pytest.raises(KernelError) as stopped:
            owner.reserve(1, {})
        assert stopped.value.code == "verification_budget_unresolved"
    assert period(path)["requests"][: plan.prior_request_count] == old
    assert period(path)["requests"][-1]["status"] == "unknown"
    with pytest.raises(KernelError) as stopped:
        with scoped(path, plan):
            pytest.fail("新未知请求不得由旧授权豁免")
    assert stopped.value.code == "verification_budget_unresolved"


@pytest.mark.parametrize(
    "scope", ["none", "wrong_grant", "wrong_suite", "missing_suite", "missing_grant"]
)
def test_default_and_other_scope_never_inherit_authorization(tmp_path, scope):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    before = path.read_bytes()
    grant, suite = plan.reverification_id, plan.suite_id
    if scope == "none":
        grant = suite = None
    elif scope == "wrong_grant":
        grant = uuid4()
    elif scope == "wrong_suite":
        suite = uuid4()
    elif scope == "missing_suite":
        suite = None
    else:
        grant = None
    with pytest.raises(KernelError):
        with VerificationBudgetLedger(path, PERIOD, reverification_id=grant, suite_id=suite):
            pytest.fail("错误范围不能读取Provider凭据")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "ledger_digest",
        "old_digest",
        "old_amount",
        "prefix_count",
        "prefix_digest",
        "suite",
        "period",
    ],
)
def test_registration_rejects_stale_or_replacement_plan_without_writing(tmp_path, change):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    update = {
        "ledger_digest": {"ledger_before_sha256": "0" * 64},
        "old_digest": {
            "carried_requests": (
                plan.carried_requests[0].model_copy(update={"request_sha256": "0" * 64}),
            )
        },
        "old_amount": {
            "carried_requests": (
                plan.carried_requests[0].model_copy(update={"reserved_cost": "1"}),
            )
        },
        "prefix_count": {"prior_request_count": 1},
        "prefix_digest": {"prior_requests_sha256": "0" * 64},
        "suite": {"suite_id": uuid4()},
        "period": {"period_id": uuid4()},
    }[change]
    if change == "suite":
        VerificationBudgetLedger.authorize_reverification(path, plan)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_reverification(path, plan.model_copy(update=update))
    assert path.read_bytes() == before


def test_total_budget_and_single_round_cap_are_both_enforced(tmp_path):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    with scoped(path, plan) as owner:
        request = owner.reserve(amount_units("20"), {})
        owner.settle(request, amount_units("20"), sent=True)
    with scoped(path, plan) as owner:
        before = path.read_bytes()
        with pytest.raises(KernelError) as stopped:
            owner.reserve(amount_units("20.000000000000000001"), {})
        assert stopped.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        request = owner.reserve(amount_units("20"), {})
        owner.settle(request, amount_units("20"), sent=True)
        with pytest.raises(KernelError):
            owner.reserve(1, {})
    assert period(path)["known_cost"] == "40.000068"
    assert period(path)["reserved_cost"] == "20.77824"


@pytest.mark.parametrize(
    "case",
    ["old_metadata", "old_status", "grant_replaced", "untagged_new", "cap_raised", "extra_field"],
)
def test_persisted_tampering_is_not_repaired(tmp_path, case):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    data = json.loads(path.read_bytes())
    current = data["periods"][0]
    grant = current["bounded_reverification"]
    if case == "old_metadata":
        current["requests"][-1]["purpose"] = "modified"
    elif case == "old_status":
        current["requests"][-1]["status"] = "reserved"
    elif case == "grant_replaced":
        grant["reverification_id"] = str(uuid4())
    elif case == "untagged_new":
        current["requests"].append(
            {
                "request_id": str(uuid4()),
                "status": "not_sent",
                "reserved_cost": "0",
                "cost_estimate": "0",
            }
        )
    elif case == "cap_raised":
        grant["maximum_cost"] = "41"
    else:
        grant["private_body"] = "fixture"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with scoped(path, plan):
            pytest.fail("危险原件不得自动修复")
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure_at", [1, 2])
def test_authorization_sync_failure_never_creates_unscoped_permission(
    tmp_path, monkeypatch, failure_at
):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    sync, count = os.fsync, 0

    def fail(descriptor):
        nonlocal count
        count += 1
        if count == failure_at:
            raise OSError
        sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(KernelError) as stopped:
        VerificationBudgetLedger.authorize_reverification(path, plan)
    assert stopped.value.code == "verification_budget_persist_failed"
    assert period(path)["reserved_cost"] == "20.77824"
    with pytest.raises(KernelError):
        with VerificationBudgetLedger(path, PERIOD):
            pytest.fail("同步失败不开放默认权限")
    assert not tuple(tmp_path.glob(".budget-*.tmp"))


def test_new_reserved_request_after_hard_exit_blocks_identical_scope(tmp_path):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    code = """
import os, sys
from pathlib import Path
from uuid import UUID
from scripts.provider_verification_budget import VerificationBudgetLedger
with VerificationBudgetLedger(Path(sys.argv[1]), UUID(sys.argv[2]),
    reverification_id=UUID(sys.argv[3]), suite_id=UUID(sys.argv[4])) as owner:
    owner.reserve(100, {})
    os._exit(17)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(path),
            str(PERIOD),
            str(plan.reverification_id),
            str(plan.suite_id),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    assert result.returncode == 17 and period(path)["requests"][-1]["status"] == "reserved"
    before = path.read_bytes()
    with pytest.raises(KernelError) as stopped:
        with scoped(path, plan):
            pytest.fail("硬退出的新预留不能视为免费")
    assert stopped.value.code == "verification_budget_unresolved" and path.read_bytes() == before


async def test_suite_cancellation_keeps_both_old_and_new_full_holds(tmp_path):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    token = CancelToken()
    with scoped(path, plan) as owner:
        provider = ObservedProvider(path, "blocked")
        task = asyncio.create_task(
            consume(GuardedVerificationProvider(provider, owner, bounds(), token))
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.closed and token.cancelled
    assert period(path)["requests"][-1]["status"] == "unknown"
    assert len([r for r in period(path)["requests"] if r["status"] == "unknown"]) == 2
    with pytest.raises(KernelError):
        with scoped(path, plan):
            pytest.fail("取消不释放新旧预留")


def test_insufficient_global_headroom_rejects_authorization_without_writing(tmp_path):
    path = ledger_file(tmp_path, known="30")
    with VerificationBudgetLedger(path, PERIOD) as owner:
        request = owner.reserve(amount_units("20.77824"), {})
        owner.settle(request, None, sent=True)
    plan = plan_for(path)
    before = path.read_bytes()
    with pytest.raises(KernelError) as stopped:
        VerificationBudgetLedger.authorize_reverification(path, plan)
    assert stopped.value.code == "verification_budget_exhausted" and path.read_bytes() == before


def test_active_reservation_cannot_be_adopted_as_old_unknown(tmp_path):
    path = ledger_file(tmp_path)
    with VerificationBudgetLedger(path, PERIOD) as owner:
        owner.reserve(amount_units("20.77824"), {})
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_reverification(path, plan_for(path))
    assert path.read_bytes() == before


def test_existing_grant_cannot_be_changed_by_reservation_owner(tmp_path):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    before = path.read_bytes()
    with scoped(path, plan) as owner:
        owner.period["bounded_reverification"]["ledger_before_sha256"] = "0" * 64
        with pytest.raises(KernelError) as stopped:
            owner.reserve(1, {})
        assert stopped.value.code == "verification_budget_persist_failed"
    assert path.read_bytes() == before


def test_guard_binding_includes_full_grant_not_only_its_identifier(tmp_path):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    with scoped(path, plan) as owner:
        guard_bounds = bounds()
        binding = guard_bounds.fingerprint(owner)
        # 摘要不能充当授权；这里只核验完整字段对恢复身份的影响。
        owner.period["bounded_reverification"]["suite_id"] = str(uuid4())
        assert guard_bounds.fingerprint(owner) != binding
    assert period(path)["bounded_reverification"]["suite_id"] == str(plan.suite_id)


@pytest.mark.parametrize("valid", [True, False])
def test_private_management_cli_is_network_free_and_never_echoes_invalid_body(
    tmp_path, capsys, valid
):
    path = held_ledger(tmp_path)
    plan = plan_for(path)
    plan_path = tmp_path / "plan.json"
    data = plan.model_dump(mode="json")
    if not valid:
        data["private_body"] = "private-fixture-not-for-output"
    plan_path.write_text(json.dumps(data))
    plan_path.chmod(0o600)
    before = path.read_bytes()
    args = ["--plan", str(plan_path), "--budget-ledger", str(path)]
    if valid:
        management.main(args)
    else:
        with pytest.raises(SystemExit) as stopped:
            management.main(args)
        assert stopped.value.code == 1 and path.read_bytes() == before
    output = capsys.readouterr()
    assert "private-fixture" not in output.out + output.err
    assert json.loads(output.out)["reason"] == (
        "authorized" if valid else "verification_reverification_invalid"
    )
    assert period(path)["reserved_cost"] == "20.77824"
