"""独立预算周期隔离历史未决费用；所有用途共用持久总额且保持默认停止。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.models.pricing import amount_units
from scripts.provider_verification_budget import VerificationBudgetLedger
from tests.evals.test_provider_verification_budget import PERIOD, ledger_file, period

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本当前限定POSIX")
NEW_PERIOD = UUID("bab39be6-7af6-4a77-9da6-fe0664e15f65")


def independent_ledger(tmp_path: Path) -> Path:
    """只构造测试私有账本，不接触实际登记收据或预算原件。"""
    directory = tmp_path / "new-period"
    directory.mkdir(mode=0o700)
    path = ledger_file(directory, allocation="60", known="0")
    data = json.loads(path.read_text())
    data["periods"][0].update(period_id=str(NEW_PERIOD), requests=[])
    path.write_text(json.dumps(data))
    return path


def test_independent_period_does_not_inherit_or_erase_old_unknown_requests(tmp_path):
    old_path = ledger_file(tmp_path, known="3.646408")
    old = json.loads(old_path.read_text())
    old["periods"][0]["requests"].extend(
        {
            "request_id": str(uuid4()),
            "status": "unknown",
            "reserved_cost": "20.77824",
            "purpose": "historical-request",
        }
        for _ in range(2)
    )
    old["periods"][0]["reserved_cost"] = "41.55648"
    old_path.write_text(json.dumps(old))
    before = old_path.read_bytes()
    with pytest.raises(KernelError) as stopped:
        with VerificationBudgetLedger(old_path, PERIOD):
            pytest.fail("旧周期未决请求仍应拒绝默认Owner")
    assert stopped.value.code == "verification_budget_unresolved"

    path = independent_ledger(tmp_path)
    with VerificationBudgetLedger(path, NEW_PERIOD) as ledger:
        assert ledger.allocation == amount_units("60")
        assert ledger.reverification_plan is None
        assert ledger.period["requests"] == []
        request = ledger.reserve(amount_units("20"), {"purpose": "R3"})
        ledger.settle(request, amount_units("1"), sent=True)
    assert period(path)["known_cost"] == "1"
    assert period(path)["reserved_cost"] == "0"
    assert old_path.read_bytes() == before
    assert period(old_path)["reserved_cost"] == "41.55648"
    assert sum(r["status"] == "unknown" for r in period(old_path)["requests"]) == 2


def test_r3_and_beta_share_one_sixty_yuan_limit_across_reopen(tmp_path):
    path = independent_ledger(tmp_path)
    for purpose, cost in (("R3", "40"), ("BETA-001", "20")):
        with VerificationBudgetLedger(path, NEW_PERIOD) as ledger:
            request = ledger.reserve(amount_units(cost), {"purpose": purpose})
            ledger.settle(request, amount_units(cost), sent=True)
    before = path.read_bytes()
    with VerificationBudgetLedger(path, NEW_PERIOD) as ledger:
        assert ledger.period["allocation"] == ledger.period["known_cost"] == "60"
        assert [r["purpose"] for r in ledger.period["requests"]] == ["R3", "BETA-001"]
        with pytest.raises(KernelError) as exhausted:
            ledger.reserve(amount_units("1"), {"purpose": "provider-verification"})
        assert exhausted.value.code == "verification_budget_exhausted"
    assert path.read_bytes() == before


@pytest.mark.parametrize("sent", [False, True])
def test_new_period_reserved_or_unknown_still_blocks_restart(tmp_path, sent):
    path = independent_ledger(tmp_path)
    with VerificationBudgetLedger(path, NEW_PERIOD) as ledger:
        request = ledger.reserve(amount_units("20.77824"), {"purpose": "R3"})
        if sent:
            ledger.settle(request, None, sent=True)
        with pytest.raises(KernelError) as stopped:
            ledger.reserve(amount_units("1"), {"purpose": "BETA-001"})
        assert stopped.value.code == "verification_budget_unresolved"
    before = path.read_bytes()
    with pytest.raises(KernelError) as restarted:
        with VerificationBudgetLedger(path, NEW_PERIOD):
            pytest.fail("新周期未决预留不得因重启或用途切换而释放")
    assert restarted.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == before
    assert period(path)["requests"][0]["status"] == ("unknown" if sent else "reserved")
    assert period(path)["known_cost"] == "0"
    assert period(path)["reserved_cost"] == "20.77824"


def test_old_identity_cannot_select_new_period(tmp_path):
    path = independent_ledger(tmp_path)
    before = path.read_bytes()
    with pytest.raises(KernelError) as wrong_period:
        with VerificationBudgetLedger(path, PERIOD):
            pytest.fail("旧UUID不得选择独立新周期")
    assert wrong_period.value.code == "verification_budget_unavailable"
    assert path.read_bytes() == before


def test_r3_and_beta_cannot_take_concurrent_owners(tmp_path):
    path = independent_ledger(tmp_path)
    with VerificationBudgetLedger(path, NEW_PERIOD) as r3:
        request = r3.reserve(amount_units("20"), {"purpose": "R3"})
        before = path.read_bytes()
        with pytest.raises(KernelError) as busy:
            with VerificationBudgetLedger(path, NEW_PERIOD):
                pytest.fail("Beta不得与R3同时取得同一账本Owner")
        assert busy.value.code == "verification_budget_busy"
        assert path.read_bytes() == before
        r3.settle(request, None, sent=False)
    with VerificationBudgetLedger(path, NEW_PERIOD) as beta:
        request = beta.reserve(amount_units("20"), {"purpose": "BETA-001"})
        beta.settle(request, None, sent=False)
    assert [r["purpose"] for r in period(path)["requests"]] == ["R3", "BETA-001"]


def test_not_sent_releases_only_its_own_reservation(tmp_path):
    path = independent_ledger(tmp_path)
    with VerificationBudgetLedger(path, NEW_PERIOD) as ledger:
        completed = ledger.reserve(amount_units("40"), {"purpose": "R3"})
        ledger.settle(completed, amount_units("40"), sent=True)
        prefix = period(path)["requests"]
        unsent = ledger.reserve(amount_units("20"), {"purpose": "BETA-001"})
        ledger.settle(unsent, None, sent=False)
        assert period(path)["requests"][:-1] == prefix
        assert period(path)["requests"][-1]["status"] == "not_sent"
        assert period(path)["requests"][-1]["cost_estimate"] == "0"
        assert period(path)["known_cost"] == "40"
        assert period(path)["reserved_cost"] == "0"
    with VerificationBudgetLedger(path, NEW_PERIOD) as ledger:
        remaining = ledger.reserve(amount_units("20"), {"purpose": "R3"})
        ledger.settle(remaining, amount_units("20"), sent=True)
    assert period(path)["known_cost"] == "60"
    assert period(path)["requests"][:1] == prefix
