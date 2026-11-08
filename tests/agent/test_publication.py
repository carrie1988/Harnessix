"""宿主纯保护端口、JSONL全文、统一失败与取消预算，不构造真实模型请求。"""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import quote

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import protect_json, protect_jsonl, protect_request_json
from harnessix.product_config.contracts import SecretReference
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope

CANARY = "model-value/+version-9"
REFERENCE = SecretReference(name="api", version="9")


def protected():
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("api", "9", "MODEL_KEY"),), environment={"MODEL_KEY": CANARY}
    )
    return SecretPublicationScope((REFERENCE,), provider)


@pytest.mark.parametrize("encoding", ["raw", "base64", "url", "hex", "unicode", "key"])
async def test_jsonl_complete_body_and_decoded_values_are_protected(encoding):
    value = {
        "raw": CANARY,
        "base64": base64.b64encode(CANARY.encode()).decode(),
        "url": quote(CANARY, safe=""),
        "hex": CANARY.encode().hex(),
    }.get(encoding, CANARY)
    line = json.dumps({CANARY: "safe"} if encoding == "key" else {"text": value})
    if encoding == "unicode":
        line = '{"text":"' + "".join(f"\\u{ord(c):04x}" for c in CANARY) + '"}'
    body = ((json.dumps({"text": "benign"}) + "\n") * 256 + line + "\n").encode()
    with protected() as scope:
        with pytest.raises(KernelError) as caught:
            await protect_jsonl(scope, body, CancelToken())
        assert caught.value.code == "public_output_secret_leak"
        assert CANARY not in str(caught.value)


async def test_jsonl_all_records_share_budget_but_are_not_one_small_native_tree():
    with protected() as scope:
        await protect_jsonl(scope, b'{"a":1}\n' * 5000, CancelToken())
        await protect_json(scope, {"summary": "completed"}, CancelToken())


@pytest.mark.parametrize(
    "body",
    [
        b'{"a":1,"a":2}\n',
        b"{broken}\n",
        b"\xff\n",
        b'{"x":NaN}\n',
    ],
)
async def test_ambiguous_or_invalid_jsonl_is_not_authorized(body):
    with protected() as scope:
        with pytest.raises(KernelError) as caught:
            await protect_jsonl(scope, body, CancelToken())
        assert caught.value.code == "public_output_protection_failed"


@pytest.mark.parametrize("mode", ["bytes", "work", "closed"])
async def test_public_protection_resource_and_capability_failure(tmp_path, monkeypatch, mode):
    from harnessix.secrets import publication

    with protected() as scope:
        if mode == "closed":
            scope.close()
        if mode == "work":
            monkeypatch.setattr(publication, "MAX_SCAN_WORK", 2)
        body = b"x" * (1024 * 1024 + 1) if mode == "bytes" else b'{"text":"safe"}\n'
        with pytest.raises(KernelError) as caught:
            await protect_jsonl(scope, body, CancelToken())
        assert caught.value.code == (
            "public_output_secret_unavailable" if mode == "closed" else "public_output_limit"
        )


@pytest.mark.parametrize("mode", ["token", "parent", "timeout", "consumed"])
async def test_public_protection_observes_new_cancel_and_sync_deadline(monkeypatch, mode):
    from harnessix.agent import publication as boundary
    from harnessix.secrets import publication

    token = CancelToken()
    clock = [boundary.monotonic()]
    original = publication._ScanBudget.step

    def step(work, depth):
        if mode == "token":
            token.cancel()
        elif mode == "parent":
            asyncio.current_task().cancel()
        elif mode == "timeout":
            clock[0] += 20
        original(work, depth)

    async def operation(scope):
        if mode == "consumed":
            task = asyncio.current_task()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.sleep(0)
        await protect_jsonl(scope, b'{"text":"safe"}\n', token)

    monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])
    monkeypatch.setattr(publication._ScanBudget, "step", step)
    with protected() as scope:
        if mode == "consumed":
            await asyncio.create_task(operation(scope))
        else:
            expected = (
                TurnCancelled
                if mode == "token"
                else asyncio.CancelledError
                if mode == "parent"
                else KernelError
            )
            with pytest.raises(expected) as caught:
                await asyncio.create_task(operation(scope))
            if mode == "timeout":
                assert caught.value.code == "public_output_timeout"


async def test_extension_diagnostic_does_not_escape_public_boundary():
    class HostGuard:
        def assert_public_json(self, value, *, checkpoint):
            raise RuntimeError(CANARY)

    with pytest.raises(KernelError) as caught:
        await protect_json(HostGuard(), {}, CancelToken())
    assert caught.value.code == "public_output_protection_failed" and CANARY not in str(
        caught.value
    )


async def test_jsonl_native_depth_has_a_deterministic_limit():
    with protected() as scope:
        with pytest.raises(KernelError) as caught:
            await protect_jsonl(scope, b"[" * 80 + b"]" * 80 + b"\n", CancelToken())
        assert caught.value.code == "public_output_limit"


def long_history(items: int) -> dict:
    """形同模型请求的长历史：每项约5个节点、几十字节。"""
    return {"history": [{"line": n, "text": "safe"} for n in range(items)]}


async def test_request_scan_bounds_nodes_by_bytes_but_keeps_depth_bytes_and_secrets():
    with protected() as scope:
        await protect_request_json(scope, long_history(20_000), CancelToken())  # 约10万节点
        for value, code in [
            ({"history": ["x" * 600_000, "y" * 600_000]}, "public_output_limit"),
            (json.loads("[" * 80 + "]" * 80), "public_output_limit"),
            ({**long_history(20_000), "tail": [CANARY]}, "public_output_secret_leak"),
        ]:
            with pytest.raises(KernelError) as caught:
                await protect_request_json(scope, value, CancelToken())
            assert caught.value.code == code


async def test_a_host_without_the_request_capability_keeps_the_stricter_native_budget():
    with protected() as scope:

        class NativeOnly:
            def assert_public_json(self, value, *, checkpoint):
                scope.assert_public_json(value, checkpoint=checkpoint)

            def assert_public_jsonl(self, body, *, checkpoint):
                scope.assert_public_jsonl(body, checkpoint=checkpoint)

        with pytest.raises(KernelError) as caught:
            await protect_request_json(NativeOnly(), long_history(20_000), CancelToken())
        assert caught.value.code == "public_output_limit"
