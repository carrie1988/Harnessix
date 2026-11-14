"""压缩点按上一次模型响应的真实输入Token校准（ADR 0132）。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.compaction_calibration import (
    calibrated_compaction,
    calibrated_limit,
    observed_input,
)
from harnessix.agent.models import Budget, Thread, Turn, TurnStatus, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttempt, ModelAttemptFinished, UsageObservation
from harnessix.context.compaction_runtime_contracts import CompactionTokenWindow
from harnessix.context.contracts import ContextInspection, ContextLimits
from harnessix.domain.models import utc_now
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
)
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.context_budget import product_context_budget
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start, observed
from tests.context.test_compaction_runtime import SequenceProvider

WINDOW = 262_144
OUTPUT = 16_384
REQUEST_CAP = 786_432


def budget(window: int | None = WINDOW, compaction: str = "half_window"):
    return product_context_budget(window, OUTPUT, compaction)  # type: ignore[arg-type]


def inspection(step: int, history: int, fixed: int) -> ContextInspection:
    limits = ContextLimits(context_window_tokens=2_000_000, reserved_output_tokens=OUTPUT)
    return ContextInspection(
        model_step=step,
        limits=limits,
        available_input_tokens=limits.available_input_tokens,
        history_tokens=history,
        tool_tokens=fixed,
        instruction_tokens=0,
        estimated_input_tokens=history + fixed,
        instruction_fingerprint="0" * 64,
    )


def attempt(step: int, input_tokens: int | None, *, status: str = "completed") -> ModelAttempt:
    now = utc_now()
    completed = status == "completed"
    return ModelAttempt(
        attempt_id=uuid4(),
        step=step,
        index=1,
        provider="test",
        requested_model="m",
        actual_model="m" if completed else None,
        response_id="r" if completed else None,
        usage=UsageObservation(completeness="complete", input_tokens=input_tokens, output_tokens=1)
        if input_tokens is not None
        else UsageObservation(),
        status=status,
        error=None if completed else {"code": "provider_transport", "message": "x"},
        started_at=now,
        finished_at=now,
    )


def turn(inspections=(), attempts=()) -> Turn:
    return Turn(
        turn_id=uuid4(),
        request_id=str(uuid4()),
        request_fingerprint="0" * 64,
        status=TurnStatus.COMPLETED,
        budget=Budget(),
        context_inspections=tuple(inspections),
        model_attempts=tuple(attempts),
        created_at=utc_now(),
    )


def thread(*turns: Turn) -> Thread:
    now = utc_now()
    return Thread(
        thread_id=uuid4(), workspace="/workspace", turns=turns, created_at=now, updated_at=now
    )


def observing(history: int, fixed: int, input_tokens: int) -> Thread:
    return thread(turn([inspection(1, history, fixed)], [attempt(1, input_tokens)]))


def test_declared_windows_carry_their_real_token_window():
    half = budget().token_window
    assert half == CompactionTokenWindow(
        window_tokens=WINDOW,
        input_limit_tokens=WINDOW - OUTPUT - 1536,
        trigger_divisor=2,
        target_divisor=4,
        max_input_bytes=REQUEST_CAP,
    )
    third = budget(compaction="third_window").token_window
    assert third is not None and (third.trigger_divisor, third.target_divisor) == (3, 6)
    # 小窗口的封顶取比例上限（每Token 8字节），不是出站请求容量。
    small = budget(65_536).token_window
    assert small is not None and small.max_input_bytes == 65_536 * 8
    assert budget(None).token_window is None


def test_without_a_window_or_an_observation_the_configuration_is_unchanged():
    config = budget().compaction_runtime(OUTPUT)
    window = budget().token_window
    observed_thread = observing(350_000, 50_000, 100_000)
    assert calibrated_compaction(config, None, observed_thread) is config
    assert calibrated_limit(None, observed_thread) is None
    for unobserved in (
        thread(),
        thread(turn()),
        # 没有Context检查（未配置Planner）或只有失败、未知用量的尝试：没有可用的观测。
        thread(turn([], [attempt(1, 100_000)])),
        thread(turn([inspection(1, 350_000, 50_000)], [attempt(1, None, status="failed")])),
        thread(turn([inspection(2, 350_000, 50_000)], [attempt(1, 100_000)])),
    ):
        assert observed_input(unobserved) is None
        assert calibrated_compaction(config, window, unobserved) is config
        assert calibrated_limit(window, unobserved) is None


def test_the_trigger_sits_at_half_of_the_real_window():
    """每Token 4字节时，按3换算的触发点（393216字节）只相当于真实窗口的42%；校准后正好一半。"""
    static = budget().compaction_runtime(OUTPUT)
    window = budget().token_window
    assert static.trigger_history_tokens == 393_216
    # 上一请求：估算40万字节（其中工具与指令5万），Provider报告10万Token。
    seen = observing(350_000, 50_000, 100_000)
    calibrated = calibrated_compaction(static, window, seen)
    assert calibrated.trigger_history_tokens == WINDOW * 4 // 2 - 50_000 == 474_288
    # 触发时整个请求（历史＋工具与指令）的真实输入正好是窗口的一半。
    assert (calibrated.trigger_history_tokens + 50_000) // 4 == WINDOW // 2
    assert calibrated.policy.target_history_tokens == WINDOW * 4 // 4 - 50_000 == 212_144
    # 输入上限按同一比例放宽，封顶于出站请求容量；摘要来源上限随之放宽。
    assert calibrated_limit(window, seen) == REQUEST_CAP
    assert calibrated.policy.max_summary_input_tokens == REQUEST_CAP
    # 摘要容量与保留组数不随校准变化。
    assert calibrated.max_summary_output_tokens == static.max_summary_output_tokens
    assert calibrated.policy.summary_reserve_tokens == static.policy.summary_reserve_tokens
    assert calibrated.policy.retain_recent_groups == static.policy.retain_recent_groups


def test_the_third_window_point_is_calibrated_the_same_way():
    third = budget(compaction="third_window")
    static = third.compaction_runtime(OUTPUT)
    calibrated = calibrated_compaction(
        static, third.token_window, observing(350_000, 50_000, 100_000)
    )
    assert calibrated.trigger_history_tokens == WINDOW * 4 // 3 - 50_000 == 299_525
    # 六分之一窗口扣除工具与指令后低于固定比例的目标：目标不收紧。
    assert WINDOW * 4 // 6 - 50_000 < static.policy.target_history_tokens == 131_072
    assert calibrated.policy.target_history_tokens == 131_072


def test_calibration_never_tightens_the_fixed_ratio_budget():
    """内容比每Token 3字节更密时不提前压缩：超窗仍由Provider超限后的被动压缩兜底，与原行为一致。"""
    static = budget().compaction_runtime(OUTPUT)
    window = budget().token_window
    dense = observing(250_000, 50_000, 150_000)
    assert calibrated_compaction(static, window, dense) == static
    limit = calibrated_limit(window, dense)
    assert limit is not None and limit < static.policy.max_summary_input_tokens


@pytest.mark.parametrize(
    ("history", "reported", "real_tokens_at_trigger"),
    [
        # 本次评测的中位比例约4.9：出站请求容量刚好先于窗口一半到达，触发点约在窗口的49.8%。
        (440_000, 100_000, 130_437),
        # 比例6.3（实测上端）：触发点停在容量允许的位置，约为窗口的39%。
        (580_000, 100_000, 101_451),
        # 用量上报异常偏小：同样停在容量允许的位置，不会随上报值无限放宽。
        (440_000, 10, 13),
    ],
)
def test_a_request_never_exceeds_the_request_size_cap(
    history: int, reported: int, real_tokens_at_trigger: int
):
    """窗口一半换算成字节超过出站请求容量时，触发点在输入上限之下留出一次退避的余量。"""
    static = budget().compaction_runtime(OUTPUT)
    window = budget().token_window
    seen = observing(history, 50_000, reported)
    calibrated = calibrated_compaction(static, window, seen)
    assert calibrated_limit(window, seen) == REQUEST_CAP
    trigger = calibrated.trigger_history_tokens
    assert trigger == (REQUEST_CAP - 50_000) * 4 // 5 == 589_145
    # 被跳过的主动压缩在历史增长四分之一后重试，此时请求仍不超过出站请求容量。
    assert trigger * 5 // 4 + 50_000 <= REQUEST_CAP
    assert (trigger + 50_000) * reported // (history + 50_000) == real_tokens_at_trigger
    # 目标与触发点保持原有比例，压缩间隔不被压短。
    assert calibrated.policy.target_history_tokens == min(
        WINDOW * (history + 50_000) // reported // 4 - 50_000, trigger // 2
    )
    assert calibrated.policy.target_history_tokens < trigger


def test_small_windows_are_bounded_by_the_plausible_ratio():
    """小窗口先被比例上限（每Token 8字节）封顶：上报异常偏小的用量不会把压缩点推到窗口之外很远。"""
    small = budget(65_536)
    bogus = calibrated_compaction(
        small.compaction_runtime(OUTPUT), small.token_window, observing(90_000, 10_000, 10)
    )
    assert bogus.trigger_history_tokens == (65_536 * 8 - 10_000) * 4 // 5 == 411_430


def test_the_latest_completed_request_of_the_thread_calibrates_the_next_turn():
    """新Turn的首个请求用上一Turn最后一次请求校准；同一Turn内取最近一步。"""
    window = budget().token_window
    earlier = turn(
        [inspection(1, 100_000, 50_000), inspection(2, 440_000, 50_000)],
        [attempt(1, 50_000), attempt(2, 100_000), attempt(3, None, status="failed")],
    )
    seen = observed_input(thread(earlier, turn()))
    assert seen is not None
    assert (seen.estimated_input_bytes, seen.history_bytes, seen.input_tokens) == (
        490_000,
        440_000,
        100_000,
    )
    assert calibrated_limit(window, thread(earlier, turn())) == REQUEST_CAP


def accounted(text: str, input_tokens: int) -> list[ProviderEvent]:
    start = attempt_start()
    return [
        start,
        ResponseStarted(response_id="response-1"),
        TextStarted(content_id="text-1"),
        TextDelta(content_id="text-1", delta=text),
        observed(start, completeness="complete", input_tokens=input_tokens, output_tokens=3),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        TextCompleted(content_id="text-1", text=text),
        ResponseCompleted(usage=Usage(input_tokens=input_tokens, output_tokens=3)),
    ]


SMALL_WINDOW = 32_768


@pytest.mark.parametrize(
    ("reported_tokens", "calibrate", "compactions"),
    [
        # fmt-2457 tk2eakD的一般形态：真实输入只占窗口的20%，字节估算却已越过“窗口一半”。
        (SMALL_WINDOW // 5, True, 0),
        # 同一历史、未校准（旧行为）：在按3字节/Token换算的触发点压缩。
        (SMALL_WINDOW // 5, False, 1),
        # 真实输入确实过半（约61%）：校准后仍然压缩。
        (20_000, True, 1),
    ],
)
async def test_a_session_compacts_by_real_tokens_not_by_the_byte_estimate(
    tmp_path: Path, reported_tokens: int, calibrate: bool, compactions: int
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "state.db"
    seeds = SequenceProvider([accounted("事实" * 1100, reported_tokens) for _ in range(8)])
    seeding = build_product_agent_context(workspace, max_output_tokens=4096)
    async with AgentRuntime(
        SQLiteSessionStore(path),
        seeds,
        async_context=seeding.context,
        compaction=seeding.compaction,
        summary_provider=seeds,
    ) as runtime:
        created = await runtime.create_thread(str(workspace))
        for index in range(8):
            seeded = await runtime.run_turn(created.thread_id, "调查实现", request_id=f"s-{index}")
            assert seeded.status == "completed" and not seeded.compactions

    final_provider = SequenceProvider([accounted("已完成验证。", reported_tokens)])
    summary = SequenceProvider([accounted("保留任务与已验证事实。", 100)])
    windowed = build_product_agent_context(
        workspace, max_output_tokens=4096, context_window_tokens=SMALL_WINDOW
    )
    assert windowed.compaction.trigger_history_tokens == 49_152
    store = SQLiteSessionStore(path)
    async with AgentRuntime(
        store,
        final_provider,
        async_context=windowed.context,
        compaction=windowed.compaction,
        compaction_window=windowed.compaction_window if calibrate else None,
        summary_provider=summary,
    ) as runtime:
        final = await runtime.run_turn(created.thread_id, "继续修复", request_id="continue")

    assert final.status == "completed"
    assert [record.status for record in final.compactions] == ["summarized"] * compactions
    assert len(summary.requests) == compactions
    stored = await store.get_thread(created.thread_id)
    assert replay(await store.events(created.thread_id)) == stored
    if compactions and calibrate:
        # 压缩计划记录的是它实际使用的策略，重放与重开都按计划自带的策略校验。
        plan = final.compactions[0].plan
        assert plan.policy.target_history_tokens >= windowed.compaction.policy.target_history_tokens
