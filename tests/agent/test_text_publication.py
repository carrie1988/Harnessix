"""真实有限模式、跨增量窗口、步骤资源预算及保护端口失败语义。"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import quote

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import begin_text_step, close_text_step, protect_text
from harnessix.product_config.contracts import SecretReference
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from tests.agent.test_publication import CANARY, protected

VALUES = (
    CANARY,
    base64.b64encode(CANARY.encode()).decode(),
    base64.b64encode(CANARY.encode()).decode().rstrip("="),
    quote(CANARY, safe=""),
    CANARY.encode().hex(),
    CANARY.encode().hex().upper(),
)


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("split", [1, 5, -1])
async def test_split_finite_pattern_never_escapes_prefix(value, split):
    with protected() as scope:
        step = await begin_text_step(scope, CancelToken())
        released = ""
        with pytest.raises(KernelError) as caught:
            released = await protect_text(step, "a", "safe:" + value[:split], CancelToken())
            await protect_text(step, "a", value[split:] + ":end", CancelToken())
        assert caught.value.code == "public_output_secret_leak"
        assert value not in released and CANARY not in str(caught.value)
        close_text_step(step, failed=True)
        assert not step._pending and not step._patterns


@pytest.mark.parametrize("chunks", [["hello"], ["你", "好", "世界"], ["", "hi", "", "there"]])
async def test_safe_utf8_and_empty_chunks_have_exact_original_final(chunks):
    with protected() as scope:
        step = await begin_text_step(scope, CancelToken())
        released = [await protect_text(step, "a", text, CancelToken()) for text in chunks]
        released.append(await protect_text(step, "a", None, CancelToken()))
        assert "".join(released) == "".join(chunks)
        close_text_step(step, failed=False)


async def test_unicode_secret_and_codepoint_cut_never_rewrite_safe_prefix():
    key = "中文凭据/测试"
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("api", "9", "K"),), environment={"K": key}
    )
    with SecretPublicationScope((SecretReference(name="api", version="9"),), provider) as scope:
        step = await begin_text_step(scope, CancelToken())
        chunks = ["🙂" * 30, "工程任务" * 40, "完成"]
        released = [await protect_text(step, "a", c, CancelToken()) for c in chunks]
        assert released[0] and released[1]
        released.append(await protect_text(step, "a", None, CancelToken()))
        assert "".join(released) == "".join(chunks)
        await protect_text(step, "b", key[:2], CancelToken())
        with pytest.raises(KernelError) as caught:
            await protect_text(step, "b", key[2:], CancelToken())
        assert caught.value.code == "public_output_secret_leak"
        close_text_step(step, failed=True)


async def test_text_blocks_are_separate_but_share_one_step_byte_budget(monkeypatch):
    from harnessix.secrets import publication

    with protected() as scope:
        monkeypatch.setattr(publication, "MAX_SCAN_BYTES", 22)
        step = await begin_text_step(scope, CancelToken())
        # 不跨不同文本块推测拼接Secret；步骤字节预算不能按块重新开始。
        await protect_text(step, "a", CANARY[:8], CancelToken())
        await protect_text(step, "b", CANARY[8:], CancelToken())
        await protect_text(step, "a", None, CancelToken())
        await protect_text(step, "b", None, CancelToken())
        with pytest.raises(KernelError) as caught:
            await protect_text(step, "c", "too long", CancelToken())
        assert caught.value.code == "public_output_limit"
        close_text_step(step, failed=True)


async def test_work_budget_is_cumulative_not_reset_per_chunk(monkeypatch):
    from harnessix.secrets import publication

    with protected() as scope:
        step = await begin_text_step(scope, CancelToken())
        await protect_text(step, "a", "a" * 20, CancelToken())
        prior_work = step._budget.work
        monkeypatch.setattr(publication, "MAX_SCAN_WORK", prior_work + 1)
        with pytest.raises(KernelError) as caught:
            await protect_text(step, "b", "b", CancelToken())
        assert caught.value.code == "public_output_limit" and step._budget.work > prior_work
        close_text_step(step, failed=True)


@pytest.mark.parametrize("case", ["scope_closed", "step_closed", "finished", "surrogate", "items"])
async def test_invalid_lifetime_and_input_is_fixed_failure(case):
    with protected() as scope:
        step = await begin_text_step(scope, CancelToken())
        if case == "scope_closed":
            scope.close()
        elif case == "step_closed":
            step.close()
        elif case == "finished":
            await protect_text(step, "a", None, CancelToken())
        elif case == "items":
            for i in range(128):
                await protect_text(step, str(i), None, CancelToken())
        with pytest.raises(KernelError) as caught:
            await protect_text(
                step,
                "a" if case != "items" else "129",
                "\ud800" if case == "surrogate" else "a",
                CancelToken(),
            )
        assert caught.value.code == (
            "public_output_secret_unavailable"
            if case == "scope_closed"
            else "public_output_limit"
            if case == "items"
            else "public_output_protection_failed"
        )
        close_text_step(step, failed=True)


@pytest.mark.parametrize("case", ["missing", "property", "factory", "methods"])
async def test_optional_stream_capability_is_fail_closed_and_diagnostics_fixed(case):
    class Host:
        @property
        def begin_public_text_step(self):
            if case == "property":
                raise RuntimeError(CANARY)
            if case == "missing":
                return None

            def create(**kwargs):
                if case == "factory":
                    raise RuntimeError(CANARY)
                return object()

            return create

    with pytest.raises(KernelError) as caught:
        await begin_text_step(Host(), CancelToken())
    assert caught.value.code == (
        "public_output_stream_capability_missing"
        if case in {"missing", "methods"}
        else "public_output_protection_failed"
    )
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("case", ["token", "parent", "timeout", "consumed"])
async def test_stream_scans_observe_cancel_and_operation_time_budget(monkeypatch, case):
    from harnessix.agent import publication as boundary
    from harnessix.secrets import publication

    token = CancelToken()
    clock = [boundary.monotonic()]
    original = publication._ScanBudget.scan_bytes

    def scan(budget, *args, **kwargs):
        if case == "token":
            token.cancel()
        elif case == "parent":
            asyncio.current_task().cancel()
        elif case == "timeout":
            clock[0] += 11
        return original(budget, *args, **kwargs)

    monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])
    with protected() as scope:
        step = await begin_text_step(scope, token)
        monkeypatch.setattr(publication._ScanBudget, "scan_bytes", scan)

        async def operation():
            if case == "consumed":
                asyncio.current_task().cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.sleep(0)
            return await protect_text(step, "a", "safe", token)

        if case == "consumed":
            await asyncio.create_task(operation())
        else:
            expected = (
                TurnCancelled
                if case == "token"
                else asyncio.CancelledError
                if case == "parent"
                else KernelError
            )
            with pytest.raises(expected) as caught:
                await asyncio.create_task(operation())
            if case == "timeout":
                assert caught.value.code == "public_output_timeout"
        close_text_step(step, failed=case != "consumed")


async def test_network_wait_is_not_part_of_synchronous_guard_deadline(monkeypatch):
    from harnessix.agent import publication as boundary

    clock = [boundary.monotonic()]
    monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])
    with protected() as scope:
        step = await begin_text_step(scope, CancelToken())
        await protect_text(step, "a", "safe", CancelToken())
        clock[0] += 60
        assert await protect_text(step, "a", None, CancelToken()) == "safe"
        close_text_step(step, failed=False)


async def test_factory_cancellation_after_creation_still_closes_guard():
    closed = []
    token = CancelToken()

    class Step:
        def feed(self, *args, **kwargs):
            return ""

        def finish(self, *args, **kwargs):
            return ""

        def close(self):
            closed.append(True)

    class Host:
        def begin_public_text_step(self, **kwargs):
            token.cancel()
            return Step()

    with pytest.raises(TurnCancelled):
        await begin_text_step(Host(), token)
    assert closed == [True]


@pytest.mark.parametrize("case", ["feed", "finish", "wrong_type", "close"])
async def test_guard_callback_exceptions_and_types_never_export_raw_diagnostic(case):
    class Step:
        def feed(self, *args, **kwargs):
            if case == "wrong_type":
                return CANARY.encode()
            raise RuntimeError(CANARY)

        def finish(self, *args, **kwargs):
            raise RuntimeError(CANARY)

        def close(self):
            raise RuntimeError(CANARY)

    with pytest.raises(KernelError) as caught:
        if case == "close":
            close_text_step(Step(), failed=False)
        else:
            await protect_text(Step(), "a", None if case == "finish" else "text", CancelToken())
    assert caught.value.code == "public_output_protection_failed" and CANARY not in str(
        caught.value
    )
    close_text_step(Step(), failed=True)


async def test_no_protection_preserves_original_compatibility():
    assert await begin_text_step(None, CancelToken()) is None
    assert await protect_text(None, "a", CANARY, CancelToken()) == CANARY
    close_text_step(None, failed=False)


async def test_binary_capability_descriptor_uses_same_diagnostic_boundary():
    from harnessix.agent.publication import protect_binary_jsonl

    class Host:
        @property
        def assert_public_binary_jsonl(self):
            raise RuntimeError(CANARY)

    with pytest.raises(KernelError) as caught:
        await protect_binary_jsonl(Host(), b"", lambda body, checkpoint: (), CancelToken())
    assert caught.value.code == "public_output_protection_failed"
    assert CANARY not in str(caught.value)
