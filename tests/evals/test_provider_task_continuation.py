"""唯一Beta承接的离线验收；仅临时账本与原生Adapter的MockTransport。"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import amount_units, format_amount
from harnessix.tools.workspace import digest
from scripts import provider_task_continuation as contracts
from scripts import run_engineering_provider_suite_budgeted as suite_host
from scripts.provider_reverification_plan import (
    CarriedVerificationRequest,
    parse_reverification_plan,
    validate_reverification_plan,
)
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import GuardedVerificationProvider
from tests.contracts.provider import model_request
from tests.evals.test_beta_verification_guard import authorized_beta_ledger, beta_bounds
from tests.evals.test_provider_reverification_v2 import (
    active_scope,
    held_ledger,
    versioned_plan,
)
from tests.evals.test_provider_verification_budget import PERIOD, period
from tests.models.wire import WireStream, chunk, frame, response

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")


@pytest.fixture(autouse=True)
def isolated_io(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("承接测试不得使用网络、凭据、真实账本或外部命令")

    for target, name in (
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (httpx.HTTPTransport, "handle_request"),
        (httpx.AsyncHTTPTransport, "handle_async_request"),
        (suite_host, "_credential"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    # OpenAI SDK的User-Agent探测在macOS会执行uname；固定测试标识，不放宽IO禁令。
    monkeypatch.setattr(platform, "platform", lambda: "offline-fixture")
    initialize = VerificationBudgetLedger.__init__

    def confined(self, path, *args, **kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        initialize(self, path, *args, **kwargs)

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", confined)
    provider_initialize = OpenAIChatProvider.__init__

    def mock_only(self, *args, **kwargs):
        assert isinstance(kwargs.get("transport"), httpx.MockTransport)
        assert kwargs.get("api_key") == "fixture-unusable"
        provider_initialize(self, *args, **kwargs)

    monkeypatch.setattr(OpenAIChatProvider, "__init__", mock_only)


def continuation_for(path, plan):
    current = period(path)
    return contracts.VerificationTaskContinuation(
        spec_version="harnessix.provider-task-continuation/v1",
        authority="budget-owner-explicit",
        continuation_id=uuid4(),
        reverification_id=plan.reverification_id,
        period_id=plan.period_id,
        task_id="BETA-001",
        maximum_requests=1,
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        prior_request_count=len(current["requests"]),
        prior_requests_sha256=digest(current["requests"]),
        carried_requests=tuple(
            CarriedVerificationRequest(
                request_id=UUID(r["request_id"]),
                reserved_cost=r["reserved_cost"],
                request_sha256=digest(r),
            )
            for r in current["requests"]
            if r["status"] == "unknown"
        ),
    )


def pending_beta(tmp_path, *, register=True):
    path, plan = authorized_beta_ledger(tmp_path)
    with VerificationBudgetLedger(
        path, plan.period_id, reverification_id=plan.reverification_id, task_id=plan.task_id
    ) as owner:
        request = owner.reserve(amount_units("0.54272"), {})
        owner.settle(request, None, sent=True)
    record = continuation_for(path, plan)
    if register:
        VerificationBudgetLedger.authorize_task_continuation(path, record)
    return path, plan, record


def scoped(path, plan, record, **overrides):
    return VerificationBudgetLedger(
        path,
        **{
            "period_id": plan.period_id,
            "reverification_id": plan.reverification_id,
            "task_id": record.task_id,
            "task_continuation_id": record.continuation_id,
            **overrides,
        },
    )


def write_data(path, data):
    path.write_text(json.dumps(data))


def test_json_roundtrip_frozen_schema_and_independent_snapshot(tmp_path):
    path, plan, record = pending_beta(tmp_path, register=False)
    text = record.model_dump_json()
    restored = contracts.parse_task_continuation(text)
    assert restored == record and type(restored.carried_requests) is tuple
    assert restored.model_dump_json() == text
    snapshot = contracts.snapshot_task_continuation(record)
    assert snapshot == record and snapshot is not record
    assert snapshot.carried_requests[0] is not record.carried_requests[0]
    contracts.validate_task_continuation(period(path), plan, restored)
    schema = record.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["maximum_requests"]["const"] == 1
    assert schema["properties"]["carried_requests"]["minItems"] == 1
    assert schema["properties"]["carried_requests"]["maxItems"] == 10000
    assert set(schema["required"]) == set(record.__dict__)
    with pytest.raises(ValueError):
        record.maximum_requests = 2
    with pytest.raises(ValueError):
        record.carried_requests[0].reserved_cost = "0"


@pytest.mark.parametrize(
    "field,value",
    [
        ("maximum_requests", True),
        ("maximum_requests", 1.0),
        ("maximum_requests", "1"),
        ("maximum_requests", 2),
        ("authority", "implicit"),
        ("task_id", "BETA-002"),
        ("spec_version", "harnessix.provider-task-continuation/v2"),
        ("continuation_id", "bad-id"),
        ("reverification_id", 1),
        ("period_id", None),
        ("prior_request_count", True),
        ("prior_request_count", 0),
        ("prior_request_count", 10001),
        ("ledger_before_sha256", "f" * 63),
        ("prior_requests_sha256", "F" * 64),
        ("suite_id", None),
        ("allocation", "60"),
        ("maximum_cost", "5"),
        ("carried_requests", []),
    ],
)
def test_parser_rejects_coercion_unknown_fields_and_wrong_version(tmp_path, field, value):
    _, _, record = pending_beta(tmp_path, register=False)
    raw = record.model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValueError):
        contracts.parse_task_continuation(json.dumps(raw))


@pytest.mark.parametrize("case", ["top_duplicate", "nested_duplicate", "bytes", "count"])
def test_parser_has_duplicate_key_and_size_boundaries(tmp_path, case):
    _, _, record = pending_beta(tmp_path, register=False)
    text = record.model_dump_json()
    if case == "top_duplicate":
        text = text.replace('"maximum_requests":1', '"maximum_requests":1,"maximum_requests":1')
    elif case == "nested_duplicate":
        text = text.replace('"reserved_cost":', '"reserved_cost":"1","reserved_cost":', 1)
    elif case == "bytes":
        text += " " * contracts.MAX_TASK_CONTINUATION_BYTES
    else:
        raw = record.model_dump(mode="json")
        raw["carried_requests"] *= 5001
        text = json.dumps(raw)
    with pytest.raises(ValueError):
        contracts.parse_task_continuation(text)


@pytest.mark.parametrize(
    "case",
    ["duck", "subclass", "uuid", "bool", "list", "nested", "extra", "missing", "bytes"],
)
def test_forged_models_rejected_before_ledger_io(tmp_path, monkeypatch, case):
    path, _, record = pending_beta(tmp_path, register=False)
    bad = record.model_copy()
    if case == "duck":
        bad = SimpleNamespace(**record.__dict__, model_dump_json=record.model_dump_json)
    elif case == "subclass":

        class Forged(contracts.VerificationTaskContinuation):
            pass

        bad = Forged.model_construct(**record.__dict__)
    elif case == "uuid":
        bad.__dict__["continuation_id"] = str(record.continuation_id)
    elif case == "bool":
        bad.__dict__["maximum_requests"] = True
    elif case == "list":
        bad.__dict__["carried_requests"] = list(record.carried_requests)
    elif case == "nested":
        request = record.carried_requests[0].model_copy(update={"request_id": "bad-id"})
        bad.__dict__["carried_requests"] = (request,)
    elif case == "extra":
        bad.__dict__["suite_id"] = uuid4()
    elif case == "missing":
        bad.__dict__.pop("maximum_requests")
    else:
        bad.__dict__["carried_requests"] *= 2000
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("伪合同必须在任何账本IO前拒绝")

    monkeypatch.setattr(VerificationBudgetLedger, "__init__", forbidden)
    with pytest.raises(KernelError) as failed:
        VerificationBudgetLedger.authorize_task_continuation(path, bad)
    assert failed.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before


def test_registration_preserves_plan_full_history_and_idempotence(tmp_path):
    path, plan, record = pending_beta(tmp_path, register=False)
    before = period(path)
    assert before["reserved_cost"] == "21.32096"
    assert validate_reverification_plan(before, plan) == amount_units("0.54272")
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    registered = path.read_bytes()
    assert json.loads(registered)["schema"] == contracts.TASK_CONTINUATION_SCHEMA
    assert period(path) == {**before, "task_continuation": record.model_dump(mode="json")}
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    assert path.read_bytes() == registered
    for replacement in (
        record.model_copy(update={"continuation_id": uuid4()}),
        record.model_copy(update={"ledger_before_sha256": "0" * 64}),
    ):
        with pytest.raises(KernelError):
            VerificationBudgetLedger.authorize_task_continuation(path, replacement)
    assert path.read_bytes() == registered
    assert path.stat().st_mode & 0o777 == 0o600
    # 原合同仍只接收一笔旧unknown，不借承接放宽原parser。
    raw_plan = plan.model_dump(mode="json")
    raw_plan["carried_requests"] = record.model_dump(mode="json")["carried_requests"]
    with pytest.raises(ValueError):
        parse_reverification_plan(json.dumps(raw_plan))


@pytest.mark.parametrize(
    "case", ["ledger", "prefix", "count", "reverify", "missing", "extra", "duplicate", "hold"]
)
def test_registration_requires_exact_entire_unresolved_set_and_prefix(tmp_path, case):
    path, _, record = pending_beta(tmp_path, register=False)
    updates = {}
    if case == "ledger":
        updates["ledger_before_sha256"] = "0" * 64
    elif case == "prefix":
        updates["prior_requests_sha256"] = "0" * 64
    elif case == "count":
        updates["prior_request_count"] = record.prior_request_count - 1
    elif case == "reverify":
        updates["reverification_id"] = uuid4()
    elif case == "missing":
        updates["carried_requests"] = record.carried_requests[:1]
    elif case == "extra":
        updates["carried_requests"] = (
            *record.carried_requests,
            record.carried_requests[0].model_copy(update={"request_id": uuid4()}),
        )
    elif case == "duplicate":
        updates["carried_requests"] = (*record.carried_requests, record.carried_requests[0])
    else:
        updates["carried_requests"] = (
            record.carried_requests[0].model_copy(update={"reserved_cost": "20.778240"}),
            record.carried_requests[1],
        )
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(
            path, record.model_copy(update=updates)
        )
    assert path.read_bytes() == before


def test_single_carried_and_generic_multiple_carried_are_supported(tmp_path):
    path, plan = authorized_beta_ledger(tmp_path)
    record = continuation_for(path, plan)
    assert len(record.carried_requests) == 1
    contracts.validate_task_continuation(period(path), plan, record)
    current = period(path)
    for _ in range(3):
        current["requests"].append(
            {
                "request_id": str(uuid4()),
                "status": "unknown",
                "reserved_cost": "0.1",
                "task_id": plan.task_id,
                "reverification_id": str(plan.reverification_id),
            }
        )
    current["reserved_cost"] = "21.07824"
    data = json.loads(path.read_text())
    data["periods"][0] = current
    write_data(path, data)
    record = continuation_for(path, plan)
    assert len(record.carried_requests) == 4
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with scoped(path, plan, record):
        pass


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_continuation_id": None},
        {"task_continuation_id": uuid4()},
        {"task_id": None},
        {"task_id": "BETA-002"},
        {"reverification_id": None},
        {"reverification_id": uuid4()},
        {"suite_id": uuid4()},
        {"period_id": uuid4()},
    ],
)
def test_only_explicit_matching_task_and_ids_can_enter(tmp_path, overrides):
    path, plan, record = pending_beta(tmp_path)
    before = path.read_bytes()
    with pytest.raises(KernelError), scoped(path, plan, record, **overrides):
        pytest.fail("身份不匹配不得获得请求能力")
    with pytest.raises(KernelError), VerificationBudgetLedger(path, PERIOD):
        pytest.fail("原Owner不能绕过承接")
    assert path.read_bytes() == before


def test_unregistered_and_suite_plan_cannot_use_continuation(tmp_path):
    path = held_ledger(tmp_path)
    plan = versioned_plan(path)
    record = continuation_for(path, plan)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(path, record)
    assert path.read_bytes() == before
    VerificationBudgetLedger.authorize_reverification(path, plan)
    record = continuation_for(path, plan)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(path, record)
    with (
        pytest.raises(KernelError),
        scoped(path, plan, record, task_id=None, suite_id=plan.suite_id),
    ):
        pytest.fail("Suite不能使用task承接")
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", ["reserved", "completed", "unknown", "not_sent"])
def test_one_reservation_consumes_permanently_for_every_terminal_and_restart(tmp_path, status):
    path, plan, record = pending_beta(tmp_path)
    before = period(path)
    with scoped(path, plan, record) as owner:
        request = owner.reserve(amount_units("0.54272"), {})
        assert period(path)["requests"][-1]["task_continuation_id"] == str(record.continuation_id)
        if status != "reserved":
            owner.settle(
                request,
                amount_units("0.1") if status == "completed" else None,
                sent=status != "not_sent",
            )
        saved = path.read_bytes()
        with pytest.raises(KernelError):
            owner.require_available()
        with pytest.raises(KernelError):
            owner.reserve(1, {})
        assert path.read_bytes() == saved
    assert period(path)["requests"][:-1] == before["requests"]
    assert period(path)["requests"][-1]["status"] == status
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with pytest.raises(KernelError), scoped(path, plan, record):
        pytest.fail("重启和相等登记不得恢复次数")
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(path, continuation_for(path, plan))
    assert path.read_bytes() == saved


def test_prior_beta_hold_still_counts_against_original_five_yuan(tmp_path):
    path, plan, record = pending_beta(tmp_path)
    with scoped(path, plan, record) as owner:
        remaining = amount_units("5") - amount_units("0.54272")
        before = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            owner.reserve(remaining + 1, {})
        assert failed.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        request = owner.reserve(remaining, {})
        assert validate_reverification_plan(owner.period, plan) == amount_units("5")
        assert owner.period["reserved_cost"] == "25.77824"
        owner.settle(request, None, sent=True)
    assert period(path)["bounded_reverification"] == plan.model_dump(mode="json")


def test_period_cap_counts_all_known_and_old_reservations(tmp_path):
    path, plan, _ = pending_beta(tmp_path, register=False)
    data = json.loads(path.read_text())
    current = data["periods"][0]
    # 让原60元上限比5元task上限更紧；重建测试前缀摘要而非改动已登记承接。
    current["requests"][0]["cost_estimate"] = "38.5"
    current["known_cost"] = "38.5"
    current["bounded_reverification"]["prior_requests_sha256"] = digest(
        current["requests"][: plan.prior_request_count]
    )
    write_data(path, data)
    plan = parse_reverification_plan(json.dumps(current["bounded_reverification"]))
    record = continuation_for(path, plan)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with scoped(path, plan, record) as owner:
        remaining = amount_units("60") - amount_units("38.5") - amount_units("21.32096")
        with pytest.raises(KernelError) as failed:
            owner.reserve(remaining + 1, {})
        assert failed.value.code == "verification_budget_exhausted"
        owner.reserve(remaining, {})
        assert amount_units(owner.period["known_cost"]) + amount_units(
            owner.period["reserved_cost"]
        ) == amount_units("60")


@pytest.mark.parametrize(
    "case", ["record", "plan", "prefix", "history", "delete", "second", "downgrade"]
)
def test_save_cannot_rewrite_record_plan_prefix_or_any_history(tmp_path, case):
    path, plan, record = pending_beta(tmp_path)
    with scoped(path, plan, record) as owner:
        request = owner.reserve(amount_units("0.5"), {})
        owner.settle(request, 0, sent=False)
        before = path.read_bytes()
        if case == "record":
            owner.period["task_continuation"]["ledger_before_sha256"] = "0" * 64
        elif case == "plan":
            owner.period["bounded_reverification"]["ledger_before_sha256"] = "0" * 64
        elif case == "prefix":
            owner.period["requests"][0]["purpose"] = "changed"
        elif case == "history":
            owner.period["requests"][-1]["purpose"] = "changed"
        elif case == "delete":
            owner.period["requests"].pop()
        elif case == "second":
            owner.period["requests"].append(
                {**owner.period["requests"][-1], "request_id": str(uuid4())}
            )
        else:
            owner.data["schema"] = "harnessix.provider-verification-budget/v1"
            owner.period.pop("task_continuation")
            owner.period["requests"][-1].pop("task_continuation_id")
        with pytest.raises(KernelError) as failed:
            owner._save()
        assert failed.value.code == "verification_budget_persist_failed"
        assert path.read_bytes() == before


@pytest.mark.parametrize("status", ["completed", "not_sent", "unknown"])
def test_save_cannot_append_terminal_without_persistent_reservation(tmp_path, status):
    path, plan, record = pending_beta(tmp_path)
    with scoped(path, plan, record) as owner:
        before = path.read_bytes()
        owner.period["requests"].append(
            {
                "request_id": str(uuid4()),
                "status": status,
                "reserved_cost": "0.1" if status == "unknown" else "0",
                **({} if status == "unknown" else {"cost_estimate": "0"}),
                "reverification_id": str(plan.reverification_id),
                "task_id": "BETA-001",
                "task_continuation_id": str(record.continuation_id),
            }
        )
        if status == "unknown":
            owner.period["reserved_cost"] = "21.42096"
        with pytest.raises(KernelError):
            owner._save()
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "case", ["unmarked", "wrong_id", "wrong_task", "suite", "prefix_tag", "old_prefix", "missing"]
)
def test_v4_reader_validates_original_prefix_and_all_suffix_ownership(tmp_path, case):
    path, plan, record = pending_beta(tmp_path)
    with scoped(path, plan, record) as owner:
        request = owner.reserve(amount_units("0.1"), {})
        owner.settle(request, 0, sent=False)
    data = json.loads(path.read_text())
    current = data["periods"][0]
    request = current["requests"][-1]
    if case == "unmarked":
        request.pop("task_continuation_id")
    elif case == "wrong_id":
        request["task_continuation_id"] = str(uuid4())
    elif case == "wrong_task":
        request["task_id"] = "BETA-002"
    elif case == "suite":
        request["suite_id"] = str(uuid4())
    elif case == "prefix_tag":
        current["requests"][0]["task_continuation_id"] = str(record.continuation_id)
    elif case == "old_prefix":
        current["requests"][0]["purpose"] = "tampered"
        # 即使重签承接摘要，也不能改写原计划冻结的更早前缀。
        current["task_continuation"]["prior_requests_sha256"] = digest(current["requests"][:-1])
    else:
        current.pop("task_continuation")
    write_data(path, data)
    before = path.read_bytes()
    with pytest.raises(KernelError) as failed:
        VerificationBudgetLedger.authorize_task_continuation(path, record)
    assert failed.value.code == "verification_budget_unavailable"
    assert path.read_bytes() == before


@pytest.mark.parametrize("schema", ["v1", "v2", "v3"])
@pytest.mark.parametrize("tag", ["record", "request"])
def test_old_formats_reject_continuation_fields(tmp_path, schema, tag):
    path = held_ledger(tmp_path)
    plan = versioned_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    scope = (
        plan
        if schema == "v1"
        else active_scope(path, plan, "rebound" if schema == "v2" else "chain")
    )
    with VerificationBudgetLedger(
        path, plan.period_id, reverification_id=plan.reverification_id, suite_id=scope.suite_id
    ):
        pass
    record = continuation_for(path, plan)
    data = json.loads(path.read_text())
    assert data["schema"] == "harnessix.provider-verification-budget/" + schema
    current = data["periods"][0]
    if tag == "record":
        current["task_continuation"] = record.model_dump(mode="json")
    else:
        current["requests"][-1]["task_continuation_id"] = str(record.continuation_id)
    write_data(path, data)
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(path, record)


@pytest.fixture(scope="module")
def frozen_reader():
    """在函数级IO封锁之前只读固定Git对象；不复制或改写历史实现。"""
    source = subprocess.run(
        [
            "git",
            "show",
            "cb40b89cae18540387f6ee417b5a9c7ae48bcbb3:scripts/provider_verification_budget.py",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    module = ModuleType("frozen_budget_reader_cb40b89c")
    exec(compile(source, "git:cb40b89c:provider_verification_budget.py", "exec"), module.__dict__)
    return module.VerificationBudgetLedger


def test_actual_old_reader_accepts_original_beta_but_fails_closed_on_v4(tmp_path, frozen_reader):
    path, plan = authorized_beta_ledger(tmp_path)
    with frozen_reader(
        path, plan.period_id, reverification_id=plan.reverification_id, task_id=plan.task_id
    ):
        pass
    record = continuation_for(path, plan)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    before = path.read_bytes()
    with (
        pytest.raises(KernelError) as failed,
        frozen_reader(
            path, plan.period_id, reverification_id=plan.reverification_id, task_id=plan.task_id
        ),
    ):
        pytest.fail("旧Reader不得把v4误认为原task授权")
    assert failed.value.code == "verification_budget_unavailable"
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["top", "inactive", "active"])
def test_save_preserves_non_request_history_in_every_period(tmp_path, target):
    path, plan, _ = pending_beta(tmp_path, register=False)
    data = json.loads(path.read_text())
    data["periods"].append({"period_id": str(uuid4()), "status": "closed", "requests": []})
    write_data(path, data)
    record = continuation_for(path, plan)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with scoped(path, plan, record) as owner:
        before = path.read_bytes()
        if target == "top":
            owner.data["history_note"] = "rewritten"
        elif target == "inactive":
            owner.data["periods"][-1]["requests"].append({"status": "unknown"})
        else:
            owner.period["history_note"] = "rewritten"
        with pytest.raises(KernelError):
            owner._save()
        assert path.read_bytes() == before


def test_save_cannot_bypass_registration_or_change_historical_reservations(tmp_path):
    path, plan, record = pending_beta(tmp_path, register=False)
    owner = VerificationBudgetLedger(path, plan.period_id)
    owner._registration_only = True
    with owner:
        before = path.read_bytes()
        owner.period["task_continuation"] = record.model_dump(mode="json")
        owner.data["schema"] = contracts.TASK_CONTINUATION_SCHEMA
        with pytest.raises(KernelError):
            owner._save()
        assert path.read_bytes() == before
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with scoped(path, plan, record) as owner:
        owner.reserve(amount_units("0.1"), {})
        before = path.read_bytes()
        owner.period["requests"][-1]["reserved_cost"] = "0.2"
        owner.period["reserved_cost"] = "21.52096"
        with pytest.raises(KernelError):
            owner._save()
        assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["replace", "fsync_file", "fsync_directory"])
def test_registration_failure_never_confers_unconfirmed_permission(tmp_path, monkeypatch, failure):
    path, plan, record = pending_beta(tmp_path, register=False)
    before = path.read_bytes()
    fsync = os.fsync

    def broken_sync(descriptor):
        import stat

        directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if directory == (failure == "fsync_directory"):
            raise OSError("fixture")
        fsync(descriptor)

    def broken_replace(*args, **kwargs):
        raise OSError("fixture")

    with monkeypatch.context() as patch:
        if failure == "replace":
            patch.setattr(os, "replace", broken_replace)
        else:
            patch.setattr(os, "fsync", broken_sync)
        with pytest.raises(KernelError) as failed:
            VerificationBudgetLedger.authorize_task_continuation(path, record)
        assert failed.value.code == "verification_budget_persist_failed"
        if failure == "fsync_directory":
            with pytest.raises(KernelError):
                VerificationBudgetLedger.authorize_task_continuation(path, record)
        else:
            assert path.read_bytes() == before
            with pytest.raises(KernelError), scoped(path, plan, record):
                pytest.fail("未发布承接不能发送")
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with scoped(path, plan, record):
        pass
    assert not list(tmp_path.glob(".budget-*.tmp"))


def test_registration_and_request_owners_are_exclusive_under_concurrency(tmp_path, monkeypatch):
    path, plan, record = pending_beta(tmp_path, register=False)
    entered, release = Event(), Event()
    save = VerificationBudgetLedger._save

    def paused_save(owner):
        entered.set()
        assert release.wait(5)
        save(owner)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(VerificationBudgetLedger, "_save", paused_save)
        first = pool.submit(VerificationBudgetLedger.authorize_task_continuation, path, record)
        try:
            assert entered.wait(5)
            with pytest.raises(KernelError) as busy:
                VerificationBudgetLedger.authorize_task_continuation(path, record)
            assert busy.value.code == "verification_budget_busy"
        finally:
            release.set()
        first.result(timeout=5)
    with scoped(path, plan, record) as owner, ThreadPoolExecutor(max_workers=1) as pool:

        def second_owner():
            with scoped(path, plan, record):
                pytest.fail("并发Owner不得进入")

        with pytest.raises(KernelError) as busy:
            pool.submit(second_owner).result(timeout=5)
        assert busy.value.code == "verification_budget_busy"
        owner.reserve(1, {})
    with pytest.raises(KernelError), scoped(path, plan, record):
        pytest.fail("关闭不归还次数")


def test_fingerprint_binds_continuation_and_is_stable_on_reopen(tmp_path):
    path, plan = authorized_beta_ledger(tmp_path)
    bounds = beta_bounds()
    with VerificationBudgetLedger(
        path, plan.period_id, reverification_id=plan.reverification_id, task_id=plan.task_id
    ) as owner:
        original = bounds.fingerprint(owner)
        request = owner.reserve(amount_units("0.54272"), {})
        owner.settle(request, None, sent=True)
    record = continuation_for(path, plan)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with scoped(path, plan, record) as owner:
        changed = bounds.fingerprint(owner)
        assert changed != original and owner.task_continuation == record
    with scoped(path, plan, record) as owner:
        assert bounds.fingerprint(owner) == changed


@pytest.mark.parametrize("case", ["completed", "unknown", "not_sent", "http_failure", "retry"])
async def test_guard_native_mock_transport_never_sends_twice(tmp_path, case):
    path, plan, record = pending_beta(tmp_path)
    before = period(path)
    bounds = beta_bounds()
    parts = [chunk({"content": "fixture"}), chunk(finish="stop")]
    if case != "unknown":
        parts.append(chunk(usage=True))
    for part in parts:
        part["model"] = bounds.model
    wire = WireStream([frame(part) for part in parts] + [b"data: [DONE]\n\n"])
    sent = 0

    def transport(request):
        nonlocal sent
        saved = period(path)["requests"][-1]
        assert saved["status"] == "reserved"
        assert saved["task_continuation_id"] == str(record.continuation_id)
        assert saved["max_attempts"] == 1
        sent += 1
        if case in {"http_failure", "retry"}:
            return httpx.Response(503)
        return response(wire)

    with scoped(path, plan, record) as owner:
        async with OpenAIChatProvider(
            OpenAIChatConfig(
                model=bounds.model,
                max_output_tokens=3072,
                max_attempts=3 if case == "retry" else 1,
                retry_delay_seconds=0,
                output_token_parameter="max_tokens",
            ),
            api_key="fixture-unusable",
            transport=httpx.MockTransport(transport),
        ) as provider:
            if case == "not_sent":
                await provider.aclose()
            token = CancelToken()
            guard = GuardedVerificationProvider(provider, owner, bounds, token)
            if case == "retry":
                with pytest.raises(KernelError):
                    _ = [e async for e in guard.stream(model_request(), CancelToken())]
            else:
                events = [e async for e in guard.stream(model_request(), CancelToken())]
                assert any(isinstance(e, ResponseCompleted) for e in events) == (
                    case == "completed"
                )
            # 新令牌和新的Guard也不能绕过账本次数，而不只是检查原取消令牌。
            second = GuardedVerificationProvider(provider, owner, bounds, CancelToken())
            with pytest.raises(KernelError):
                _ = [e async for e in second.stream(model_request(), CancelToken())]
    assert sent == (0 if case == "not_sent" else 1)
    current = period(path)
    assert current["requests"][:-1] == before["requests"]
    status = case if case in {"completed", "not_sent"} else "unknown"
    assert current["requests"][-1]["status"] == status
    held = amount_units("21.32096") + (bounds.maximum_units if status == "unknown" else 0)
    assert current["reserved_cost"] == format_amount(held)
    with pytest.raises(KernelError), scoped(path, plan, record):
        pytest.fail("重开不能产生第二个传输请求")


@pytest.mark.parametrize("after_publish", [False, True])
async def test_guard_does_not_send_when_reservation_persistence_fails(
    tmp_path, monkeypatch, after_publish
):
    path, plan, record = pending_beta(tmp_path)
    before = path.read_bytes()
    sent = 0

    def transport(request):
        nonlocal sent
        sent += 1
        return httpx.Response(503)

    def failure(*args, **kwargs):
        raise OSError("fixture")

    sync = os.fsync

    def directory_failure(descriptor):
        import stat

        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failure()
        sync(descriptor)

    bounds = beta_bounds()
    with scoped(path, plan, record) as owner:
        async with OpenAIChatProvider(
            OpenAIChatConfig(model=bounds.model, max_output_tokens=3072, max_attempts=1),
            api_key="fixture-unusable",
            transport=httpx.MockTransport(transport),
        ) as provider:
            guard = GuardedVerificationProvider(provider, owner, bounds, CancelToken())
            with monkeypatch.context() as patch:
                patch.setattr(
                    os,
                    "fsync" if after_publish else "replace",
                    directory_failure if after_publish else failure,
                )
                with pytest.raises(KernelError) as failed:
                    _ = [e async for e in guard.stream(model_request(), CancelToken())]
                assert failed.value.code == "verification_budget_persist_failed"
            assert guard.suite_cancel.cancelled
            with pytest.raises(KernelError):
                owner.reserve(1, {})
    assert sent == 0
    if after_publish:
        assert period(path)["requests"][-1]["status"] == "reserved"
        with pytest.raises(KernelError), scoped(path, plan, record):
            pytest.fail("发布后未可靠确认的预留不能重用")
    else:
        assert path.read_bytes() == before
