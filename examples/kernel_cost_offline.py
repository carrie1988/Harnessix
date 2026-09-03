"""实际 SDK/Kernel 的离线成本验收；费率与计费上下文均为虚构夹具，不是真实账单。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from examples.kernel_openai_offline import handle
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.costs import CostReport, bind_price, build_cost_report
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import BillingContext, PriceSnapshot
from harnessix.session.sqlite import SQLiteSessionStore


async def main() -> None:
    os.environ["HARNESSIX_COST_FIXTURE_KEY"] = "not-a-real-credential"
    config = OpenAIChatConfig(
        base_url="https://provider.invalid/v1",
        model="offline-fixture",
        api_key_env="HARNESSIX_COST_FIXTURE_KEY",
    )
    price = PriceSnapshot.model_validate(
        {
            "version": "fictional-example-v1",
            "source_url": "https://prices.invalid/fixture",
            "billing_provider": "fixture-platform",
            "region": "fixture-region",
            "service_tier": "fixture-tier",
            "inference_mode": "fixture-mode",
            "model": "offline-fixture",
            "currency": "USD",
            "valid_from": datetime(2020, 1, 1, tzinfo=UTC),
            "valid_until": datetime(2100, 1, 1, tzinfo=UTC),
            "input_tokens_min": 0,
            "input_tokens_max": 10000,
            "input_price": {
                "kind": "partitioned",
                "uncached_per_million": "2",
                "cache_read_per_million": "0.5",
                "cache_creation_per_million": "3",
                "cache_write_ttl": None,
            },
            "output_per_million": "4",
        }
    )
    context = BillingContext(
        billing_provider="fixture-platform",
        region="fixture-region",
        service_tier="fixture-tier",
        inference_mode="fixture-mode",
    )
    with tempfile.TemporaryDirectory(prefix="harnessix-cost-") as directory:
        store = SQLiteSessionStore(Path(directory) / "s.db")
        async with OpenAIChatProvider(config, transport=httpx.MockTransport(handle)) as provider:
            async with AgentRuntime(store, provider) as runtime:
                thread = await runtime.create_thread(directory)
                turn = await runtime.run_turn(thread.thread_id, "成本验收", request_id="cost")
        bindings = tuple(bind_price(a, price, context) for a in turn.model_attempts)
        report = build_cost_report(turn, bindings)
        assert report.summary.completeness == "complete"
        assert report.summary.totals[0].known_amount == "0.0000495"
        assert CostReport.model_validate_json(report.model_dump_json()) == report
        assert report == build_cost_report(
            replay(await store.events(thread.thread_id)).turns[-1], bindings
        )
    print(
        "虚构费率与上下文；估算 Token 成本：0.0000495 USD；JSON/Replay 重算一致；真实 API 调用：0"
    )


if __name__ == "__main__":
    asyncio.run(main())
