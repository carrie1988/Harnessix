"""追加候选不覆盖旧授权或刷新额度；只使用临时账本及离线Provider。"""

from __future__ import annotations

import json
import os
import socket
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.models.pricing import amount_units, format_amount
from harnessix.tools.workspace import digest
from scripts import authorize_provider_reverification as management
from scripts.provider_reverification_chain import VerificationCandidateBinding
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import BailianVerificationBounds
from tests.evals.test_provider_reverification_rebinding import (
    authorized_ledger,
    binding_for,
    new_scope,
)
from tests.evals.test_provider_verification_budget import period

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")


@pytest.fixture(autouse=True)
def isolated_io(tmp_path, monkeypatch):
    """禁止真实网络，且每个管理Owner只能打开本测试临时目录内的账本。"""

    def forbidden(*_, **__):
        pytest.fail("候选链验证不得联网")

    for target, name in (
        (socket, "create_connection"),
        (socket.socket, "connect"),
        (httpx.Client, "send"),
        (httpx.AsyncClient, "send"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    initialize = VerificationBudgetLedger.__init__

    def confined(self, path, *args, **kwargs):
        assert path.resolve().is_relative_to(tmp_path.resolve())
        initialize(self, path, *args, **kwargs)

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", confined)


def rebound(tmp_path):
    path, plan = authorized_ledger(tmp_path)
    original = binding_for(path, plan)
    VerificationBudgetLedger.rebind_reverification(path, original)
    with new_scope(path, original) as ledger:
        request = ledger.reserve(amount_units("1.807932"), {"purpose": "completed-candidate"})
        ledger.settle(request, amount_units("1.807932"), sent=True)
    return path, plan, original


def candidate_for(path, plan, previous, sequence=1):
    current = period(path)
    charged = sum(
        amount_units(request["reserved_cost"]) + amount_units(request.get("cost_estimate", "0"))
        for request in current["requests"][plan.prior_request_count :]
    )
    return VerificationCandidateBinding(
        spec_version="harnessix.provider-reverification-binding/v2",
        authority="budget-owner-explicit",
        binding_id=uuid4(),
        reverification_id=plan.reverification_id,
        period_id=plan.period_id,
        previous_suite_id=previous.suite_id,
        suite_id=uuid4(),
        previous_binding_sha256=digest(previous.model_dump(mode="json")),
        sequence=sequence,
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        original_plan_sha256=digest(plan.model_dump(mode="json")),
        prior_request_count=len(current["requests"]),
        prior_requests_sha256=digest(current["requests"]),
        charged_cost=format_amount(charged),
        remaining_cost=format_amount(amount_units(plan.maximum_cost) - charged),
    )


def test_three_candidates_preserve_all_prior_records_and_one_cumulative_cap(tmp_path):
    path, plan, original = rebound(tmp_path)
    before = deepcopy(period(path))
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    first = path.read_bytes()
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    assert path.read_bytes() == first
    with new_scope(path, candidate) as ledger:
        assert ledger.reverification_binding == original
        assert ledger.active_reverification_binding == candidate
        request = ledger.reserve(amount_units("1"), {"purpose": "next-candidate"})
        ledger.settle(request, amount_units("0.5"), sent=True)
    next_candidate = candidate_for(path, plan, candidate, 2)
    VerificationBudgetLedger.append_reverification_binding(path, next_candidate)
    with new_scope(path, next_candidate) as ledger:
        assert ledger.active_reverification_binding == next_candidate
    after = period(path)
    assert after["requests"][: len(before["requests"])] == before["requests"]
    assert after["bounded_reverification"] == before["bounded_reverification"]
    assert after["reverification_binding"] == before["reverification_binding"]
    assert after["allocation"] == "70"
    assert after["reserved_cost"] == "20.77824"
    assert next_candidate.remaining_cost == "37.472932"
    assert json.loads(path.read_bytes())["schema"] == "harnessix.provider-verification-budget/v3"
    assert after["reverification_binding_chain"] == [
        candidate.model_dump(mode="json"),
        next_candidate.model_dump(mode="json"),
    ]
    for revoked in (plan, original, candidate):
        with pytest.raises(KernelError):
            with new_scope(path, revoked):
                pytest.fail("历史候选不得重新获得请求权限")


@pytest.mark.parametrize(
    "field",
    [
        "period_id",
        "reverification_id",
        "previous_suite_id",
        "previous_binding_sha256",
        "original_plan_sha256",
        "prior_requests_sha256",
        "ledger_before_sha256",
        "binding_id",
        "suite_id",
        "sequence",
        "prior_request_count",
        "charged_cost",
        "remaining_cost",
    ],
)
def test_candidate_field_drift_rejects_before_any_ledger_write(tmp_path, field):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    changed = {
        "period_id": uuid4(),
        "reverification_id": uuid4(),
        "previous_suite_id": uuid4(),
        "previous_binding_sha256": "f" * 64,
        "original_plan_sha256": "f" * 64,
        "prior_requests_sha256": "f" * 64,
        "ledger_before_sha256": "f" * 64,
        "binding_id": original.binding_id,
        "suite_id": original.suite_id,
        "sequence": 2,
        "prior_request_count": candidate.prior_request_count - 1,
        "charged_cost": "0",
        "remaining_cost": "40",
    }
    bad = VerificationCandidateBinding.model_validate({**candidate.__dict__, field: changed[field]})
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.append_reverification_binding(path, bad)
    assert path.read_bytes() == before


@pytest.mark.parametrize("state", ["reserved", "unknown"])
def test_new_unresolved_request_blocks_append_without_releasing_old_or_new_hold(tmp_path, state):
    path, plan, original = rebound(tmp_path)
    with new_scope(path, original) as ledger:
        request = ledger.reserve(amount_units("1"), {"purpose": "new-unresolved"})
        if state == "unknown":
            ledger.settle(request, None, sent=True)
    candidate = candidate_for(path, plan, original)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.append_reverification_binding(path, candidate)
    assert path.read_bytes() == before
    assert period(path)["reserved_cost"] == "21.77824"


def test_reservation_after_append_still_uses_remaining_original_forty_yuan(tmp_path):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    before = path.read_bytes()
    with new_scope(path, candidate) as ledger:
        with pytest.raises(KernelError) as stopped:
            ledger.reserve(amount_units("38"), {"purpose": "over-original-cap"})
        assert stopped.value.code == "verification_budget_exhausted"
    assert path.read_bytes() == before


@pytest.mark.parametrize("attack", ["remove", "replace", "wrong-request-suite", "old-request"])
def test_in_memory_history_rewrite_is_not_published(tmp_path, attack):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    before = path.read_bytes()
    with new_scope(path, candidate) as ledger:
        if attack == "remove":
            ledger.period["reverification_binding_chain"] = []
        elif attack == "replace":
            ledger.period["reverification_binding_chain"][0]["suite_id"] = str(uuid4())
        elif attack == "wrong-request-suite":
            ledger.period["requests"][-1]["suite_id"] = str(candidate.suite_id)
        else:
            ledger.period["requests"][0]["purpose"] = "rewritten-history"
        with pytest.raises(KernelError):
            ledger._save()
    assert path.read_bytes() == before


def test_original_single_switch_api_remains_single_switch_after_extension(tmp_path):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.rebind_reverification(path, binding_for(path, plan))
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", True),
        ("sequence", 0),
        ("sequence", 33),
        ("sequence", "1"),
        ("prior_request_count", False),
        ("remaining_cost", "-1"),
        ("remaining_cost", "1e2"),
        ("remaining_cost", 38),
        ("authority", "automatic"),
        ("spec_version", "harnessix.provider-reverification-binding/v1"),
        ("binding_id", "1f4220b8-e906-4852-b7ad-805e9e2e203c"),
    ],
)
def test_candidate_contract_rejects_wrong_original_types_and_bounds(tmp_path, field, value):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    with pytest.raises(ValidationError):
        VerificationCandidateBinding.model_validate({**candidate.__dict__, field: value})
    forged = candidate.model_copy(update={field: value})
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.append_reverification_binding(path, forged)
    assert path.read_bytes() == before


@pytest.mark.parametrize("attack", ["extra", "missing", "binding-subclass"])
def test_forged_or_extended_models_are_revalidated(tmp_path, attack):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    if attack == "extra":
        bad = candidate.model_copy(update={"extra": "private-fixture"})
    elif attack == "missing":
        fields = dict(candidate.__dict__)
        del fields["previous_binding_sha256"]
        bad = VerificationCandidateBinding.model_construct(**fields)
    else:

        class DerivedBinding(VerificationCandidateBinding):
            pass

        bad = DerivedBinding.model_validate(candidate.__dict__)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.append_reverification_binding(path, bad)
    assert path.read_bytes() == before


def test_budget_fingerprint_binds_actual_active_candidate_and_preserves_v2(tmp_path):
    from tests.evals.test_provider_verification_budget import bounds

    path, plan, original = rebound(tmp_path)
    limit: BailianVerificationBounds = bounds()
    with new_scope(path, original) as ledger:
        old_fingerprint = limit.fingerprint(ledger)
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    with new_scope(path, candidate) as ledger:
        current_fingerprint = limit.fingerprint(ledger)
        assert current_fingerprint != old_fingerprint
        assert current_fingerprint == limit.fingerprint(ledger)
    with new_scope(path, candidate) as ledger:
        assert current_fingerprint == limit.fingerprint(ledger)


def test_new_unknown_after_append_stops_current_and_further_candidates(tmp_path):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    with new_scope(path, candidate) as ledger:
        request = ledger.reserve(amount_units("1"), {"purpose": "new-unknown"})
        ledger.settle(request, None, sent=True)
        with pytest.raises(KernelError) as stopped:
            ledger.reserve(amount_units("0.1"), {"purpose": "must-stop"})
        assert stopped.value.code == "verification_budget_unresolved"
    before = path.read_bytes()
    for scope in (plan, original, candidate):
        with pytest.raises(KernelError):
            with new_scope(path, scope):
                pytest.fail("新增未决后不得进入请求Owner")
    with pytest.raises(KernelError):
        VerificationBudgetLedger.append_reverification_binding(
            path, candidate_for(path, plan, candidate, 2)
        )
    assert path.read_bytes() == before


def test_failed_sync_retains_registration_and_retry_only_confirms(tmp_path, monkeypatch):
    import stat

    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    original_sync = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("private-fixture")
        return original_sync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(KernelError):
        VerificationBudgetLedger.append_reverification_binding(path, candidate)
    after = path.read_bytes()
    assert period(path)["reverification_binding_chain"] == [candidate.model_dump(mode="json")]
    monkeypatch.setattr(os, "fsync", original_sync)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    assert path.read_bytes() == after


def test_cli_explicit_append_publishes_only_finite_metadata(tmp_path, capsys):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    config = tmp_path / "candidate-plan.json"
    config.write_text(candidate.model_dump_json())
    config.chmod(0o600)
    management.main(["--append-binding-plan", str(config), "--budget-ledger", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert result == {"reason": "candidate_appended", "binding_id": str(candidate.binding_id)}
    assert str(path) not in json.dumps(result)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_chain_cannot_be_hidden_under_old_schema(tmp_path, version):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    changed = json.loads(path.read_bytes())
    changed["schema"] = "harnessix.provider-verification-budget/" + version
    path.write_text(json.dumps(changed))
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with new_scope(path, candidate):
            pytest.fail("旧Schema不得忽略新候选链")
    assert path.read_bytes() == before


def test_unscoped_owner_remains_blocked_by_original_unknown_after_append(tmp_path):
    path, plan, original = rebound(tmp_path)
    candidate = candidate_for(path, plan, original)
    VerificationBudgetLedger.append_reverification_binding(path, candidate)
    before = path.read_bytes()
    with pytest.raises(KernelError) as stopped:
        with VerificationBudgetLedger(path, plan.period_id):
            pytest.fail("默认Owner不能继承未决豁免")
    assert stopped.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == before
