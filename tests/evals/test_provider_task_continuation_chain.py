"""V2追加承接的增量回归；复用V1隔离边界，只操作临时账本和MockTransport。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts import provider_task_continuation as contracts
from scripts.provider_reverification_plan import (
    parse_reverification_plan,
    validate_reverification_plan,
)
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import GuardedVerificationProvider
from tests.contracts.provider import model_request
from tests.evals.test_beta_verification_guard import beta_bounds
from tests.evals.test_provider_task_continuation import (
    continuation_for,
    pending_beta,
    scoped,
    write_data,
)
from tests.evals.test_provider_task_continuation import (
    isolated_io as isolated_io,
)
from tests.evals.test_provider_verification_budget import period
from tests.models.wire import WireStream, chunk, frame, response

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")


def successor_for(path, plan, previous):
    return contracts.VerificationTaskContinuationV2(
        **{
            **continuation_for(path, plan).model_dump(),
            "spec_version": "harnessix.provider-task-continuation/v2",
            "maximum_requests": 4,
            "previous_continuation_id": previous.continuation_id,
        }
    )


def pending_chain(tmp_path, *, register=True):
    path, plan, previous = pending_beta(tmp_path)
    with scoped(path, plan, previous) as owner:
        request = owner.reserve(amount_units("0.54272"), {})
        owner.settle(request, None, sent=True)
    record = successor_for(path, plan, previous)
    if register:
        VerificationBudgetLedger.authorize_task_continuation(path, record)
    return path, plan, previous, record


def test_v2_roundtrip_and_frozen_schema_leave_v1_contract_unchanged(tmp_path):
    _, _, previous, record = pending_chain(tmp_path, register=False)
    restored = contracts.parse_task_continuation(record.model_dump_json())
    assert type(restored) is contracts.VerificationTaskContinuationV2
    assert restored == record
    snapshot = contracts.snapshot_task_continuation(record)
    assert snapshot == record and snapshot is not record
    assert snapshot.carried_requests[0] is not record.carried_requests[0]
    schema = record.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["maximum_requests"]["const"] == 4
    assert schema["properties"]["previous_continuation_id"]["format"] == "uuid"
    assert set(schema["required"]) == set(record.__dict__)
    with pytest.raises(ValueError):
        record.maximum_requests = 1
    assert contracts.parse_task_continuation(previous.model_dump_json()) == previous
    assert previous.model_json_schema()["properties"]["maximum_requests"]["const"] == 1
    assert "previous_continuation_id" not in type(previous).model_fields


@pytest.mark.parametrize("field", ["maximum_requests", "prior_request_count"])
@pytest.mark.parametrize("value", [True, 4.0])
def test_v2_parser_rejects_bool_and_float_counts(tmp_path, field, value):
    _, _, _, record = pending_chain(tmp_path, register=False)
    raw = record.model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValueError):
        contracts.parse_task_continuation(json.dumps(raw))


def test_successors_only_append_and_preserve_every_prior_record_and_cost(tmp_path):
    path, plan, previous, record = pending_chain(tmp_path, register=False)
    assert period(path)["requests"][-1]["status"] == "unknown"
    for _ in range(2):
        before = json.loads(path.read_text())
        old_period = before["periods"][0]
        VerificationBudgetLedger.authorize_task_continuation(path, record)
        expected = {
            **before,
            "schema": contracts.TASK_CONTINUATION_CHAIN_SCHEMA,
            "periods": [
                {
                    **old_period,
                    "task_continuation_chain": [
                        *old_period.get("task_continuation_chain", []),
                        record.model_dump(mode="json"),
                    ],
                }
            ],
        }
        assert json.loads(path.read_text()) == expected
        assert period(path)["task_continuation"] == previous.model_dump(mode="json")
        with scoped(path, plan, record) as owner:
            assert owner.task_continuation == record
            request = owner.reserve(amount_units("0.1"), {})
            owner.settle(request, None, sent=True)
        record = successor_for(path, plan, record)


def test_four_reservations_include_not_sent_and_survive_reopen_and_idempotence(tmp_path):
    path, plan, _, record = pending_chain(tmp_path)
    prefix = period(path)["requests"]
    statuses = ["completed", "not_sent", "completed", "not_sent"]
    for index, status in enumerate(statuses, start=1):
        with scoped(path, plan, record) as owner:
            request = owner.reserve(amount_units("0.1"), {})
            owner.settle(
                request,
                amount_units("0.1") if status == "completed" else 0,
                sent=status != "not_sent",
            )
            if index == 4:
                saved = path.read_bytes()
                with pytest.raises(KernelError):
                    owner.require_available()
                with pytest.raises(KernelError):
                    owner.reserve(1, {})
                assert path.read_bytes() == saved
        saved = path.read_bytes()
        VerificationBudgetLedger.authorize_task_continuation(path, record)
        assert path.read_bytes() == saved
        assert len(period(path)["requests"]) == record.prior_request_count + index
    with pytest.raises(KernelError), scoped(path, plan, record):
        pytest.fail("重开及幂等登记不能恢复第五次许可")
    current = period(path)
    assert current["requests"][: record.prior_request_count] == prefix
    assert [r["status"] for r in current["requests"][record.prior_request_count :]] == statuses
    assert all(
        r["task_continuation_id"] == str(record.continuation_id)
        for r in current["requests"][record.prior_request_count :]
    )
    assert path.read_bytes() == saved


@pytest.mark.parametrize("identity", ["unscoped", "original_task", "v1", "suite"])
def test_old_owners_and_suite_cannot_use_latest_continuation(tmp_path, identity):
    path, plan, previous, record = pending_chain(tmp_path)
    before = path.read_bytes()
    overrides = {
        "original_task": {"task_continuation_id": None},
        "v1": {"task_continuation_id": previous.continuation_id},
        "suite": {"suite_id": uuid4()},
    }
    owner = (
        VerificationBudgetLedger(path, plan.period_id)
        if identity == "unscoped"
        else scoped(path, plan, record, **overrides[identity])
    )
    with pytest.raises(KernelError), owner:
        pytest.fail("原Owner、原V1和Suite不得获得V2请求能力")
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", ["unknown", "reserved"])
def test_new_unresolved_request_stops_immediately_even_with_three_slots_left(tmp_path, status):
    path, plan, _, record = pending_chain(tmp_path)
    before = period(path)
    with scoped(path, plan, record) as owner:
        request = owner.reserve(amount_units("0.1"), {})
        if status == "unknown":
            owner.settle(request, None, sent=True)
        saved = path.read_bytes()
        with pytest.raises(KernelError):
            owner.require_available()
        with pytest.raises(KernelError):
            owner.reserve(1, {})
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with pytest.raises(KernelError), scoped(path, plan, record):
        pytest.fail("未决请求不能通过重开或幂等登记绕过")
    assert path.read_bytes() == saved
    assert period(path)["requests"][:-1] == before["requests"]
    assert period(path)["requests"][-1]["status"] == status
    assert amount_units(period(path)["reserved_cost"]) == (
        amount_units(before["reserved_cost"]) + amount_units("0.1")
    )


@pytest.mark.parametrize("case", ["wrong_predecessor", "same_uuid", "stale_ledger"])
def test_registration_rejects_invalid_successor_without_writing(tmp_path, case):
    path, _, previous, record = pending_chain(tmp_path, register=False)
    updates = {
        "wrong_predecessor": {"previous_continuation_id": uuid4()},
        "same_uuid": {"continuation_id": previous.continuation_id},
        "stale_ledger": {"ledger_before_sha256": "0" * 64},
    }
    before = path.read_bytes()
    with pytest.raises(KernelError) as failed:
        VerificationBudgetLedger.authorize_task_continuation(
            path, record.model_copy(update=updates[case])
        )
    assert failed.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["v1", "chain", "old_request", "new_terminal"])
def test_save_rejects_rewriting_contracts_and_request_history(tmp_path, target):
    path, plan, _, record = pending_chain(tmp_path)
    with scoped(path, plan, record) as owner:
        request = owner.reserve(amount_units("0.1"), {})
        owner.settle(request, 0, sent=False)
        before = path.read_bytes()
        if target == "v1":
            owner.period["task_continuation"]["ledger_before_sha256"] = "0" * 64
        elif target == "chain":
            owner.period["task_continuation_chain"][0]["ledger_before_sha256"] = "0" * 64
        else:
            index = record.prior_request_count - 1 if target == "old_request" else -1
            owner.period["requests"][index]["purpose"] = "rewritten"
        with pytest.raises(KernelError) as failed:
            owner._save()
        assert failed.value.code == "verification_budget_persist_failed"
        assert path.read_bytes() == before


@pytest.mark.parametrize("cap", ["task", "period"])
def test_v2_retains_original_five_yuan_task_and_sixty_yuan_period_caps(tmp_path, cap):
    path, plan, previous = pending_beta(tmp_path, register=False)
    if cap == "period":
        # 在任何承接登记前构造更紧的原周期额度，不重签已经冻结的历史。
        data = json.loads(path.read_text())
        current = data["periods"][0]
        current["requests"][0]["cost_estimate"] = current["known_cost"] = "38"
        current["bounded_reverification"]["prior_requests_sha256"] = digest(
            current["requests"][: plan.prior_request_count]
        )
        write_data(path, data)
        plan = parse_reverification_plan(json.dumps(current["bounded_reverification"]))
        previous = continuation_for(path, plan)
    VerificationBudgetLedger.authorize_task_continuation(path, previous)
    with scoped(path, plan, previous) as owner:
        request = owner.reserve(amount_units("0.54272"), {})
        owner.settle(request, None, sent=True)
    record = successor_for(path, plan, previous)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    before = period(path)
    with scoped(path, plan, record) as owner:
        assert validate_reverification_plan(owner.period, plan) == amount_units("1.08544")
        remaining = (
            amount_units("5") - amount_units("1.08544")
            if cap == "task"
            else amount_units("60") - amount_units("38") - amount_units("21.86368")
        )
        saved = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            owner.reserve(remaining + 1, {})
        assert failed.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == saved
        owner.reserve(remaining, {})
        if cap == "task":
            assert validate_reverification_plan(owner.period, plan) == amount_units("5")
        else:
            assert amount_units(owner.period["known_cost"]) + amount_units(
                owner.period["reserved_cost"]
            ) == amount_units("60")
    assert period(path)["requests"][:-1] == before["requests"]
    assert period(path)["allocation"] == plan.allocation == "60"
    assert plan.maximum_cost == "5"
    assert period(path)["bounded_reverification"] == before["bounded_reverification"]


@pytest.fixture(scope="module")
def frozen_v4_reader():
    """沿用V1测试做法：仅在IO封锁前读取固定Git对象，不复制历史源码到工作区。"""
    source = subprocess.run(
        [
            "git",
            "show",
            "805e6677cff80769415841ca25c8015eb6918a32:scripts/provider_verification_budget.py",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    module = ModuleType("frozen_budget_reader_805e6677")
    exec(compile(source, "git:805e6677:provider_verification_budget.py", "exec"), module.__dict__)
    return module.VerificationBudgetLedger


def test_actual_v4_reader_rejects_v5_and_current_reader_rejects_downgrade(
    tmp_path, frozen_v4_reader
):
    path, plan, previous = pending_beta(tmp_path)
    with frozen_v4_reader(
        path,
        plan.period_id,
        reverification_id=plan.reverification_id,
        task_id=plan.task_id,
        task_continuation_id=previous.continuation_id,
    ):
        pass
    with scoped(path, plan, previous) as owner:
        request = owner.reserve(amount_units("0.1"), {})
        owner.settle(request, None, sent=True)
    record = successor_for(path, plan, previous)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    before = path.read_bytes()
    legacy = frozen_v4_reader(path, plan.period_id)
    # 跳过授权/未决门禁，确保是旧reader的schema校验拒绝，而非V1次数耗尽。
    legacy._registration_only = True
    with pytest.raises(KernelError) as failed, legacy:
        pytest.fail("v4 reader不得静默接收v5")
    assert failed.value.code == "verification_budget_unavailable"
    assert path.read_bytes() == before
    data = json.loads(before)
    data["schema"] = contracts.TASK_CONTINUATION_SCHEMA
    write_data(path, data)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(path, record)
    assert path.read_bytes() == before


@pytest.mark.parametrize("not_sent_first", [False, True])
async def test_native_mock_transport_never_sends_fifth_request(tmp_path, not_sent_first):
    path, plan, _, record = pending_chain(tmp_path)
    prefix = period(path)["requests"]
    bounds = beta_bounds()
    sent = 0

    def transport(request):
        nonlocal sent
        saved = period(path)["requests"][-1]
        assert saved["status"] == "reserved"
        assert saved["task_continuation_id"] == str(record.continuation_id)
        assert saved["max_attempts"] == 1
        sent += 1
        parts = [chunk({"content": "fixture"}), chunk(finish="stop"), chunk(usage=True)]
        for part in parts:
            part["model"] = bounds.model
        return response(WireStream([frame(part) for part in parts] + [b"data: [DONE]\n\n"]))

    with scoped(path, plan, record) as owner:
        for index in range(5):
            async with OpenAIChatProvider(
                OpenAIChatConfig(model=bounds.model, max_output_tokens=3072, max_attempts=1),
                api_key="fixture-unusable",
                transport=httpx.MockTransport(transport),
            ) as provider:
                not_sent = not_sent_first and index == 0
                if not_sent:
                    await provider.aclose()
                guard = GuardedVerificationProvider(provider, owner, bounds, CancelToken())
                if index < 4:
                    events = [e async for e in guard.stream(model_request(), CancelToken())]
                    assert any(isinstance(e, ResponseCompleted) for e in events) == (not not_sent)
                else:
                    saved, prior_sent = path.read_bytes(), sent
                    with pytest.raises(KernelError):
                        _ = [e async for e in guard.stream(model_request(), CancelToken())]
                    assert sent == prior_sent and path.read_bytes() == saved
                    assert guard.suite_cancel.cancelled
    assert sent == (3 if not_sent_first else 4)
    assert period(path)["requests"][: record.prior_request_count] == prefix
    suffix = period(path)["requests"][record.prior_request_count :]
    assert [r["status"] for r in suffix] == (
        ["not_sent", "completed", "completed", "completed"] if not_sent_first else ["completed"] * 4
    )
