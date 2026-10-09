"""单Suite发送节奏不重试、不改预算；取消等待不得产生费用记录或遗留锁。"""

import asyncio
from unittest.mock import patch

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import (
    GuardedVerificationProvider,
    VerificationRequestPacer,
)
from tests.evals.test_provider_verification_budget import (
    PERIOD,
    ObservedProvider,
    bounds,
    consume,
    ledger_file,
    model_request,
    period,
)


@pytest.mark.parametrize("value", [-1, 61, float("nan"), float("inf"), True, "3", None])
def test_invalid_intervals(value) -> None:
    with pytest.raises(KernelError, match="验证发送间隔无效") as raised:
        VerificationRequestPacer(value)
    assert raised.value.code == "verification_request_pacing_invalid"


@pytest.mark.asyncio
async def test_zero_interval_preserves_no_wait_admission() -> None:
    pacer = VerificationRequestPacer(0)
    with patch("scripts.provider_verification_guard.asyncio.sleep") as sleep:
        await pacer.wait(CancelToken(), CancelToken())
        await pacer.wait(CancelToken(), CancelToken())
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_shared_pacer_spaces_separate_provider_instances(tmp_path) -> None:
    path = ledger_file(tmp_path)
    starts = []

    class RecordedPacer(VerificationRequestPacer):
        async def _wait(self):
            await super()._wait()
            starts.append(self._next_start - self.minimum_interval_seconds)

    pacer = RecordedPacer(0.025)

    with VerificationBudgetLedger(path, PERIOD) as ledger:
        for _ in range(3):
            provider = ObservedProvider(path)
            await consume(
                GuardedVerificationProvider(provider, ledger, bounds(), CancelToken(), pacer)
            )
            assert provider.sent == 1
    assert len(starts) == 3
    assert all(
        later - before >= 0.025 for before, later in zip(starts[:-1], starts[1:], strict=True)
    )
    assert len(period(path)["requests"]) == 4
    assert all(request["status"] == "completed" for request in period(path)["requests"])


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["turn", "suite", "task"])
async def test_cancel_wait_before_reservation_and_release_lock(tmp_path, source) -> None:
    path = ledger_file(tmp_path)
    before = path.read_bytes()
    pacer = VerificationRequestPacer(3)
    await pacer.wait(CancelToken(), CancelToken())
    turn, suite = CancelToken(), CancelToken()
    provider = ObservedProvider(path)
    entered = asyncio.Event()
    real_sleep = asyncio.sleep

    async def observed_sleep(delay):
        entered.set()
        await real_sleep(delay)

    with VerificationBudgetLedger(path, PERIOD) as ledger:
        guard = GuardedVerificationProvider(provider, ledger, bounds(), suite, pacer)
        with patch("scripts.provider_verification_guard.asyncio.sleep", observed_sleep):

            async def run():
                return [
                    event
                    async for event in guard.stream(
                        model_request(),
                        turn,
                    )
                ]

            operation = asyncio.create_task(run())
            await asyncio.wait_for(entered.wait(), 1)
            if source == "task":
                operation.cancel()
            else:
                (turn if source == "turn" else suite).cancel()
            with pytest.raises(asyncio.CancelledError if source == "task" else TurnCancelled):
                await asyncio.wait_for(operation, 1)
        assert not pacer._lock.locked()
        assert path.read_bytes() == before
        assert provider.sent == 0
    assert path.read_bytes() == before


def test_pacing_binding_preserves_default_and_changes_explicit_policy(tmp_path) -> None:
    path = ledger_file(tmp_path)
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        policy = bounds()
        original = policy.fingerprint(ledger)
        assert policy.fingerprint(ledger, pacer=VerificationRequestPacer(0)) == original
        assert policy.fingerprint(ledger, pacer=VerificationRequestPacer(3)) != original


@pytest.mark.asyncio
async def test_cancel_queued_admission_does_not_release_another_owner_lock() -> None:
    entered = asyncio.Event()

    class ObservedPacer(VerificationRequestPacer):
        async def _wait(self):
            entered.set()
            await super()._wait()

    pacer = ObservedPacer(0)
    token = CancelToken()
    async with pacer._lock:
        operation = asyncio.create_task(pacer.wait(token, CancelToken()))
        await asyncio.wait_for(entered.wait(), 1)
        token.cancel()
        with pytest.raises(TurnCancelled):
            await asyncio.wait_for(operation, 1)
        assert pacer._lock.locked()
    assert not pacer._lock.locked()
    await asyncio.wait_for(pacer.wait(CancelToken(), CancelToken()), 1)


@pytest.mark.asyncio
async def test_expired_price_after_wait_still_refuses_before_reservation(tmp_path) -> None:
    path = ledger_file(tmp_path)
    before = path.read_bytes()
    provider = ObservedProvider(path)
    policy = bounds()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        with patch.object(
            type(policy), "checkpoint", side_effect=KernelError("price_fixture", "fixed")
        ):
            with pytest.raises(KernelError, match="fixed"):
                await consume(
                    GuardedVerificationProvider(
                        provider, ledger, policy, CancelToken(), VerificationRequestPacer(0)
                    )
                )
    assert provider.sent == 0
    assert path.read_bytes() == before
