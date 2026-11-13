"""付费交互模式下服务方故障后的自动重试次数来自产品配置（ADR 0144）。

默认5次；配置里的service_failure_retries可以改，0表示不自动重试。走真实的产品服务端装配，只替换网络Provider。
"""

from __future__ import annotations

import asyncio
import io

import pytest

from harnessix.agent.errors import AgentFailure
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.product_config.server import SERVICE_FAILURE_RETRIES, run_product_stdio
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from tests.agent.attempt_helpers import accounted_answer, attempt_start
from tests.product_config.conftest import write_config
from tests.product_config.test_product_rollback_sdk import ProductBundle
from tests.product_config.test_server_and_cli import _credentials


def _interrupted_then_answer():
    """第一次请求的流式响应中途断开（用量未知），重试的第二次请求正常完成。"""
    first, second = attempt_start(), attempt_start(index=2)
    interrupted = ModelAttemptFinished(
        attempt_id=first.attempt_id,
        outcome="failed",
        error=AgentFailure(code="provider_transport", message="连接中断", retryable=True),
    )
    return [first, interrupted, *accounted_answer(start=second)]


async def _run(tmp_path, config, monkeypatch, steps=None) -> tuple[str, str | None, int]:
    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    root.mkdir()
    path, bundle = write_config(tmp_path / "config.json", config), ProductBundle([])
    seen: dict[str, object] = {}

    async def build(*_args, **_kwargs):
        return bundle

    async def drive(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(root), request_id="thread")
            bundle.steps = ((steps or _interrupted_then_answer)(),)
            await client.start_turn(thread.thread_id, "回答一句话", request_id="turn")
            for _ in range(2000):
                turn = (await client.get_thread(thread.thread_id)).latest_turn
                if turn is not None and turn.status in {"completed", "failed", "interrupted"}:
                    break
                await asyncio.sleep(0.01)
            seen["status"] = turn.status
            seen["error"] = turn.error.code if turn.error else None
        finally:
            await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    await run_product_stdio(
        config_path=path,
        profile_id=None,
        workspace=root,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    return str(seen["status"]), seen["error"], len(bundle.requests)


async def test_default_configuration_retries_after_a_network_interruption(
    tmp_path, config, monkeypatch
):
    assert SERVICE_FAILURE_RETRIES == 5 and config.service_failure_retries is None
    status, error, _ = await _run(tmp_path, config, monkeypatch)
    assert (status, error) == ("completed", None)


@pytest.mark.parametrize(
    ("retries", "expected"),
    [(3, ("completed", None)), (0, ("failed", "provider_transport"))],
)
async def test_configured_retry_count_is_used(tmp_path, config, monkeypatch, retries, expected):
    chosen = config.model_copy(update={"service_failure_retries": retries})
    status, error, _ = await _run(tmp_path, chosen, monkeypatch)
    assert (status, error) == expected
