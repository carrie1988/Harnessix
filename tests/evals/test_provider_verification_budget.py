"""验证宿主持久预算：已知/未知分离、独占、取消及正式Adapter线协议。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest, ResponseCompleted, ResponseFailed
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import amount_units, format_amount
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import (
    MODEL,
    BailianVerificationBounds,
    GuardedVerificationProvider,
)
from tests.contracts.provider import model_request as wire_request
from tests.models.wire import WireStream, chunk, frame, response

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本当前限定POSIX")
PERIOD = UUID("f130e2de-e696-4da9-b6c6-2e0aa77e1298")


def ledger_file(tmp_path: Path, allocation="70", known="0.000068") -> Path:
    tmp_path.chmod(0o700)
    request = {
        "request_id": str(uuid4()),
        "status": "completed",
        "reserved_cost": "0",
        "cost_estimate": known,
        "purpose": "previous-probe",
    }
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schema": "harnessix.provider-verification-budget/v1",
                "provider": "aliyun-bailian",
                "currency": "CNY",
                "periods": [
                    {
                        "period_id": str(PERIOD),
                        "status": "active",
                        "allocation": allocation,
                        "known_cost": known,
                        "reserved_cost": "0",
                        "requests": [request],
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def period(path: Path) -> dict:
    return json.loads(path.read_text())["periods"][0]


def bounds() -> BailianVerificationBounds:
    now = datetime.now(UTC)
    return BailianVerificationBounds(now - timedelta(minutes=1), now + timedelta(hours=1), 128)


def model_request() -> ModelRequest:
    return ModelRequest(
        thread_id=uuid4(), turn_id=uuid4(), step=1, history=(), tools=(), budget=Budget()
    )


class ObservedProvider:
    def __init__(self, path: Path, mode="complete"):
        self.path, self.mode, self.sent, self.closed = path, mode, 0, False
        self.entered = asyncio.Event()

    async def stream(self, request, cancel):
        # 与官方Adapter一致：实际网络IO位于Started事件之后。
        try:
            assert period(self.path)["requests"][-1]["status"] == "reserved"
            if self.mode == "not_sent":
                yield ResponseFailed(code="invalid_request")
                return
            attempt = uuid4()
            yield ModelAttemptStarted(
                attempt_id=attempt,
                step=request.step,
                index=1,
                provider="openai_chat",
                requested_model=MODEL,
            )
            self.sent += 1
            self.entered.set()
            if self.mode == "exception":
                raise RuntimeError("opaque-vendor-fixture-not-for-logs")
            if self.mode == "blocked":
                await asyncio.Event().wait()
            if self.mode == "retry":
                yield ModelAttemptStarted(
                    attempt_id=uuid4(),
                    step=request.step,
                    index=2,
                    provider="openai_chat",
                    requested_model=MODEL,
                )
                self.sent += 1
            counts = 0 if self.mode == "zero" else 13
            output = 0 if self.mode == "zero" else 129 if self.mode == "overshoot" else 1
            if self.mode != "missing":
                usage = UsageObservation(
                    completeness="partial" if self.mode == "partial" else "complete",
                    input_tokens=counts,
                    output_tokens=output,
                )
                yield ModelUsageObserved(
                    attempt_id=attempt,
                    usage=usage,
                    actual_model="other-model" if self.mode == "alias" else MODEL,
                )
            yield ModelAttemptFinished(attempt_id=attempt, outcome="completed")
            yield ResponseCompleted(usage=Usage(input_tokens=counts, output_tokens=output))
            if self.mode == "after_terminal":
                yield ResponseCompleted()
        finally:
            self.closed = True


async def consume(guard):
    return [event async for event in guard.stream(model_request(), CancelToken())]


async def test_reservation_precedes_send_and_settlement_precedes_success(tmp_path):
    path = ledger_file(tmp_path)
    before = period(path)["requests"][0]
    host_cancel = CancelToken()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path)
        guard = GuardedVerificationProvider(provider, ledger, bounds(), host_cancel)
        async for event in guard.stream(model_request(), CancelToken()):
            if isinstance(event, ResponseCompleted):
                assert period(path)["requests"][-1]["status"] == "completed"
                assert period(path)["reserved_cost"] == "0"
    current = period(path)
    assert current["requests"][0] == before and current["allocation"] == "70"
    assert current["known_cost"] == "0.000136"
    assert provider.sent == 1 and provider.closed and not host_cancel.cancelled
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "mode", ["missing", "partial", "alias", "overshoot", "exception", "retry", "after_terminal"]
)
async def test_unknown_or_invalid_attempt_holds_full_reservation_and_stops_suite(tmp_path, mode):
    path = ledger_file(tmp_path)
    cancellation = CancelToken()
    expected = bounds()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path, mode)
        with pytest.raises((KernelError, RuntimeError)):
            await consume(GuardedVerificationProvider(provider, ledger, expected, cancellation))
        assert cancellation.cancelled and provider.sent == 1 and provider.closed
        assert period(path)["requests"][-1]["status"] == "unknown"
        assert period(path)["reserved_cost"] == format_amount(expected.maximum_units)
        assert period(path)["known_cost"] == "0.000068"
        with pytest.raises(KernelError):
            ledger.reserve(expected.maximum_units, {})
    with pytest.raises(KernelError):
        with VerificationBudgetLedger(path, PERIOD):
            pytest.fail("重启不得自动退款或继续未决请求")


@pytest.mark.parametrize("mode", ["not_sent", "zero"])
async def test_not_sent_and_explicit_complete_zero_are_not_unknown(tmp_path, mode):
    path = ledger_file(tmp_path)
    token = CancelToken()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path, mode)
        await consume(GuardedVerificationProvider(provider, ledger, bounds(), token))
    assert not token.cancelled and period(path)["reserved_cost"] == "0"
    assert period(path)["known_cost"] == "0.000068"
    assert period(path)["requests"][-1]["status"] == (
        "not_sent" if mode == "not_sent" else "completed"
    )


async def test_insufficient_money_and_expired_price_never_send(tmp_path):
    path = ledger_file(tmp_path, allocation="0.01")
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path)
        token = CancelToken()
        with pytest.raises(KernelError) as failed:
            await consume(GuardedVerificationProvider(provider, ledger, bounds(), token))
        assert failed.value.code == "verification_budget_exhausted" and provider.sent == 0
        assert len(period(path)["requests"]) == 1 and token.cancelled
        now = datetime.now(UTC)
        expired = BailianVerificationBounds(now - timedelta(hours=2), now - timedelta(hours=1), 128)
        with pytest.raises(KernelError) as failed:
            await consume(GuardedVerificationProvider(provider, ledger, expired, CancelToken()))
        assert failed.value.code == "verification_price_unavailable"
        assert len(period(path)["requests"]) == 1


async def test_parent_task_cancel_drains_source_and_keeps_ambiguous_reservation(tmp_path):
    path = ledger_file(tmp_path)
    token = CancelToken()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path, "blocked")
        guard = GuardedVerificationProvider(provider, ledger, bounds(), token)
        task = asyncio.create_task(consume(guard))
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.closed and token.cancelled
        assert period(path)["requests"][-1]["status"] == "unknown"


async def test_suite_cancel_alone_interrupts_blocked_provider_and_drains_source(tmp_path):
    path = ledger_file(tmp_path)
    token = CancelToken()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path, "blocked")
        task = asyncio.create_task(
            consume(GuardedVerificationProvider(provider, ledger, bounds(), token))
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        token.cancel()
        with pytest.raises(TurnCancelled):
            await asyncio.wait_for(task, timeout=1)
        assert provider.closed and period(path)["requests"][-1]["status"] == "unknown"


def test_owner_conflict_and_external_replacement_are_fail_closed(tmp_path):
    path = ledger_file(tmp_path)
    with VerificationBudgetLedger(path, PERIOD) as first:
        with pytest.raises(KernelError) as failed:
            with VerificationBudgetLedger(path, PERIOD):
                pytest.fail("第二Owner不得进入")
        assert failed.value.code == "verification_budget_busy"
        replacement = path.with_suffix(".other")
        original = path.read_bytes()
        replacement.write_bytes(original + b" ")
        replacement.chmod(0o600)
        replacement.replace(path)
        with pytest.raises(KernelError) as failed:
            first.reserve(bounds().maximum_units, {})
        assert failed.value.code == "verification_budget_persist_failed"
        assert path.read_bytes() == original + b" "


@pytest.mark.parametrize(
    "case", ["mode", "hardlink", "symlink", "wrong_period", "inconsistent_total", "duplicate_json"]
)
def test_unsafe_or_inconsistent_existing_ledger_is_not_repaired(tmp_path, case):
    path = ledger_file(tmp_path)
    selected = PERIOD
    if case == "mode":
        path.chmod(0o644)
    elif case == "hardlink":
        os.link(path, tmp_path / "alias")
    elif case == "symlink":
        saved = tmp_path / "saved"
        path.rename(saved)
        path.symlink_to(saved)
    elif case == "wrong_period":
        selected = uuid4()
    elif case == "inconsistent_total":
        data = json.loads(path.read_text())
        data["periods"][0]["known_cost"] = "0"
        path.write_text(json.dumps(data))
    else:
        path.write_text('{"schema":"a","schema":"b"}')
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with VerificationBudgetLedger(path, selected):
            pytest.fail("危险原件不得自动修复")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (32_000, "0.128016"),
        (32_001, "0.19203"),
        (128_000, "0.768024"),
        (128_001, "1.28005"),
        (256_000, "2.56004"),
        (256_001, "5.12022"),
    ],
)
def test_price_tier_boundaries_use_exact_integer_amounts(tokens, expected):
    observed = UsageObservation(completeness="complete", input_tokens=tokens, output_tokens=1)
    assert bounds().cost_units(observed) == amount_units(expected)


def test_body_or_credentials_cannot_be_added_to_request_metadata(tmp_path):
    path = ledger_file(tmp_path)
    before = path.read_bytes()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        with pytest.raises(KernelError):
            ledger.reserve(bounds().maximum_units, {"prompt": "private-fixture"})
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure_at", [1, 2, 3, 4])
async def test_reservation_or_settlement_sync_failure_never_publishes_success(
    tmp_path, monkeypatch, failure_at
):
    path = ledger_file(tmp_path)
    sync = os.fsync
    calls = 0

    def fail_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise OSError("sync-fixture-not-for-logs")
        sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_sync)
    token = CancelToken()
    seen = []
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path)
        with pytest.raises(KernelError) as failed:
            async for event in GuardedVerificationProvider(
                provider, ledger, bounds(), token
            ).stream(model_request(), CancelToken()):
                seen.append(event)
        assert failed.value.code == "verification_budget_persist_failed"
    assert token.cancelled and not any(isinstance(event, ResponseCompleted) for event in seen)
    assert provider.sent == (0 if failure_at <= 2 else 1)
    current = period(path)
    if failure_at == 1:
        assert len(current["requests"]) == 1
    elif failure_at in {2, 3}:
        assert current["requests"][-1]["status"] == "reserved"
        assert current["known_cost"] == "0.000068"
        with pytest.raises(KernelError) as failed:
            with VerificationBudgetLedger(path, PERIOD):
                pytest.fail("同步不确定的预留不能自动退款")
        assert failed.value.code == "verification_budget_unresolved"
    else:
        # replace已发生但目录fsync失败：不发布成功；重启以实际可读原字节为准。
        assert current["requests"][-1]["status"] == "completed"
        assert current["known_cost"] == "0.000136"
    assert not tuple(tmp_path.glob(".budget-*.tmp"))


@pytest.mark.parametrize("case", ["complete", "missing_usage", "wrong_model", "truncated"])
async def test_native_adapter_wire_is_reserved_before_transport_and_closed(
    tmp_path, monkeypatch, case
):
    path = ledger_file(tmp_path)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    parts = [
        chunk({"role": "assistant", "content": "fixture"}),
        chunk(finish="stop"),
        chunk(usage=True),
    ]
    for part in parts:
        part["model"] = "other-model" if case == "wrong_model" else MODEL
    if case == "missing_usage":
        parts.pop()
    wire = WireStream(
        [frame(part) for part in parts] + ([] if case == "truncated" else [b"data: [DONE]\n\n"])
    )
    sent = 0

    def transport(request):
        nonlocal sent
        assert period(path)["requests"][-1]["status"] == "reserved"
        payload = json.loads(request.content)
        assert payload["model"] == MODEL and payload["max_tokens"] == 128
        sent += 1
        return response(wire)

    token = CancelToken()
    seen = []
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        async with OpenAIChatProvider(
            OpenAIChatConfig(
                model=MODEL,
                max_output_tokens=128,
                max_attempts=1,
                retry_delay_seconds=0,
                output_token_parameter="max_tokens",
            ),
            api_key="verification-fixture-key",
            transport=httpx.MockTransport(transport),
        ) as provider:
            guard = GuardedVerificationProvider(provider, ledger, bounds(), token)
            if case == "complete":
                seen = [event async for event in guard.stream(wire_request(), CancelToken())]
            else:
                # 失败终态可以是正式Adapter的ResponseFailed或Guard的有限异常。
                try:
                    seen = [event async for event in guard.stream(wire_request(), CancelToken())]
                except KernelError:
                    pass
    assert sent == 1 and wire.closed
    if case == "complete":
        assert isinstance(seen[-1], ResponseCompleted) and not token.cancelled
        assert period(path)["known_cost"] == "0.00014"
    else:
        assert token.cancelled and period(path)["requests"][-1]["status"] == "unknown"
        assert not any(isinstance(event, ResponseCompleted) for event in seen)


@pytest.mark.parametrize("cost,sent", [(True, True), (0.5, True), (-1, True), (1, False), (0, 1)])
def test_invalid_settlement_types_or_unsent_cost_preserve_original_reservation(
    tmp_path, cost, sent
):
    path = ledger_file(tmp_path)
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        identity = ledger.reserve(bounds().maximum_units, {})
        before = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            ledger.settle(identity, cost, sent=sent)
        assert failed.value.code == "verification_budget_settlement_invalid"
        assert path.read_bytes() == before


@pytest.mark.parametrize("output", [True, 0, 4097])
def test_invalid_output_bound_is_not_implicitly_coerced(output):
    expected = bounds()
    with pytest.raises(KernelError):
        BailianVerificationBounds(expected.valid_from, expected.valid_until, output).checkpoint()
