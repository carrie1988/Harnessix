import pytest

from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.costs import CostReport, bind_price, build_cost_report
from harnessix.session.sqlite import SQLiteSessionStore
from tests.models.attempt_helpers import CANARY, KEY_ENV, Adapter
from tests.models.pricing_helpers import context, price


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("scenario", ["success", "retry", "failure_after_usage"])
async def test_real_sdk_to_durable_cost_report_replay(tmp_path, monkeypatch, kind, scenario):
    monkeypatch.setenv(KEY_ENV, CANARY)
    for name in ("OPENAI_CUSTOM_HEADERS", "ANTHROPIC_CUSTOM_HEADERS"):
        monkeypatch.delenv(name, raising=False)
    adapter = Adapter(kind)
    parts = adapter.detailed_frames()
    if scenario == "failure_after_usage" and kind == "openai":
        parts.pop()
    wire = adapter.wire.WireStream(parts, fail=scenario == "failure_after_usage")
    requests = []

    def handle(request):
        requests.append(request)
        return (
            adapter.error()
            if scenario == "retry" and len(requests) == 1
            else adapter.wire.response(wire)
        )

    store = SQLiteSessionStore(tmp_path / "s.db")
    async with adapter.provider(wire, handler=handle) as provider:
        async with AgentRuntime(store, provider) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id, "成本报告不保存此用户消息", request_id="r"
            )
    bindings = tuple(bind_price(a, price(), context()) for a in turn.model_attempts)
    report = build_cost_report(turn, bindings)
    assert wire.closed and len(requests) == (2 if scenario == "retry" else 1)
    assert report.summary.completeness == ("partial" if scenario == "retry" else "complete")
    assert report.summary.totals[0].known_amount == "0.000025"
    assert turn.status == ("failed" if scenario == "failure_after_usage" else "completed")
    encoded = report.model_dump_json()
    assert CANARY not in encoded and "成本报告不保存此用户消息" not in encoded
    assert CostReport.model_validate_json(encoded) == report
    restored = replay(await store.events(thread.thread_id)).turns[-1]
    assert build_cost_report(restored, bindings) == report
