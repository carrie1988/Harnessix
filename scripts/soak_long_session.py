"""通过真实Agent Runtime执行长会话，并发布可独立重算的低敏Soak证据。"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, Thread, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context import ContextEngine, ContextFragment, ContextFragmentKind, ContextLimits
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.session.sqlite import SQLiteSessionStore
from scripts.soak_attempt import attempt_scope
from scripts.soak_context_proof import SoakContextProof, SoakContextTurn, SoakEventMarker
from scripts.soak_environment import read_environment
from scripts.soak_manifest import (
    SoakFileWatermarks,
    SoakLoad,
    SoakManifest,
    SoakManifestV2,
    SoakProfileReference,
)
from scripts.soak_provider import SoakProvider, SoakSummaryProvider
from scripts.soak_rss import read_peak_rss
from scripts.soak_run_common import (
    check_release_revision,
    file_bytes,
    latency_sample,
    publish_measured_run,
    rss_sample,
)
from scripts.soak_samples import SoakSample


async def run_long_session(
    evidence_root: Path,
    *,
    code_revision: str,
    turn_count: int,
    warmup_count: int,
    seed: int = 0,
    turn_timeout_seconds: float = 30.0,
) -> tuple[Path, SoakManifest]:
    """保留v1规模诊断入口及其历史证据合同。"""

    directory, manifest = await _run_long_session(
        evidence_root,
        code_revision=code_revision,
        turn_count=turn_count,
        warmup_count=warmup_count,
        seed=seed,
        turn_timeout_seconds=turn_timeout_seconds,
        context_mode=False,
    )
    assert isinstance(manifest, SoakManifest) and not isinstance(manifest, SoakManifestV2)
    return directory, manifest


async def run_long_session_context(
    evidence_root: Path,
    *,
    code_revision: str,
    turn_count: int,
    warmup_count: int,
    seed: int = 0,
    turn_timeout_seconds: float = 30.0,
    threshold_profile_ref: SoakProfileReference | None = None,
) -> tuple[Path, SoakManifestV2]:
    """执行真实Context与Compaction并发布v2低敏证明。"""

    directory, manifest = await _run_long_session(
        evidence_root,
        code_revision=code_revision,
        turn_count=turn_count,
        warmup_count=warmup_count,
        seed=seed,
        turn_timeout_seconds=turn_timeout_seconds,
        context_mode=True,
        threshold_profile_ref=threshold_profile_ref,
    )
    assert isinstance(manifest, SoakManifestV2)
    return directory, manifest


def _context_runtime() -> tuple[ContextEngine, CompactionRuntimeConfig]:
    """固定大于压缩目标的窗口；触发阈值不由实测结果回填。"""

    context = ContextEngine(
        ContextLimits(
            context_window_tokens=32_768,
            reserved_output_tokens=1024,
            provider_overhead_tokens=0,
            safety_margin_tokens=0,
        ),
        (
            ContextFragment(
                kind=ContextFragmentKind.RUNTIME_INSTRUCTION,
                source="soak-runtime",
                content="执行固定长会话容量测试，保留已确认工程事实。",
            ),
        ),
    )
    compaction = CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=2500,
            summary_reserve_tokens=1024,
            max_summary_input_tokens=100_000,
            retain_recent_groups=1,
            min_savings_tokens=256,
        ),
        trigger_history_tokens=3000,
        max_summary_output_tokens=128,
    )
    return context, compaction


def _context_proof(
    run_id: str,
    persisted: Thread,
    events: list[AgentEvent],
    *,
    warmup_count: int,
    summary_request_count: int,
) -> SoakContextProof:
    """从Replay一致的持久投影和事件构造无正文覆盖事实。"""

    windows = {window.compaction_id for window in persisted.compaction_windows}
    turns = tuple(
        SoakContextTurn(
            ordinal=index,
            phase="warmup" if index <= warmup_count else "measure",
            model_steps=turn.model_steps,
            context_inspections=len(turn.context_inspections),
            history_inspections=len(turn.model_history_inspections),
            compaction_plans=len(turn.compactions),
            summary_attempts=sum(item.attempt is not None for item in turn.compactions),
            completed_summaries=sum(item.status == "summarized" for item in turn.compactions),
            window_activations=sum(item.plan.compaction_id in windows for item in turn.compactions),
        )
        for index, turn in enumerate(persisted.turns, start=1)
    )
    event_counts = Counter(event.payload.type for event in events)
    total_context = sum(turn.context_inspections for turn in turns)
    total_history = sum(turn.history_inspections for turn in turns)
    total_compactions = sum(turn.completed_summaries for turn in turns)
    if (
        len(events) != persisted.sequence
        or event_counts["context_prepared"] != total_context
        or event_counts["model_history_prepared"] != total_history
        or any(
            event_counts[kind] != total_compactions
            for kind in (
                "compaction_planned",
                "compaction_attempt_started",
                "compaction_usage_observed",
                "compaction_attempt_finished",
                "compaction_summarized",
                "compaction_window_activated",
            )
        )
        or event_counts["compaction_rejected"] != 0
        or summary_request_count != total_compactions
    ):
        raise KernelError("soak_context_coverage_invalid", "Context持久事件与压缩账本不一致")
    # Turn投影不含ordinal；它只由本次持久顺序确定，不发布UUID。
    ordinal_by_id = {turn.turn_id: index for index, turn in enumerate(persisted.turns, start=1)}
    try:
        return SoakContextProof(
            spec_version="harnessix.soak-context-proof/v1",
            run_id=run_id,
            turns=turns,
            events=tuple(
                SoakEventMarker(
                    sequence=event.sequence,
                    turn_ordinal=ordinal_by_id.get(event.turn_id, 0),
                    kind=event.payload.type,
                )
                for event in events
            ),
            final_window_count=len(persisted.compaction_windows),
            final_event_sequence=persisted.sequence,
        )
    except ValueError:
        raise KernelError(
            "soak_context_coverage_invalid", "正式长会话未完成Context压缩覆盖"
        ) from None


async def _run_long_session(
    evidence_root: Path,
    *,
    code_revision: str,
    turn_count: int,
    warmup_count: int,
    seed: int,
    turn_timeout_seconds: float,
    context_mode: bool,
    threshold_profile_ref: SoakProfileReference | None = None,
) -> tuple[Path, SoakManifest | SoakManifestV2]:
    """复用同一采样/Attempt链；模式差异只在Runtime配置与证据版本。"""

    if (
        not isinstance(code_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None
        or not isinstance(evidence_root, Path)
        or type(turn_count) is not int
        or not 1 <= turn_count <= 10_000
        or type(warmup_count) is not int
        or not 0 <= warmup_count <= 1_000
        or type(seed) is not int
        or seed < 0
        or not 0 < turn_timeout_seconds <= 300
        or (threshold_profile_ref is not None and (not context_mode or turn_count < 1000))
    ):
        raise KernelError("soak_load_invalid", "长会话Soak负载参数无效")
    if turn_count >= 1000:
        check_release_revision(code_revision)

    run_id = uuid4().hex
    with attempt_scope(
        evidence_root, run_id=run_id, code_revision=code_revision, scenario_id="long_session"
    ) as attempt:
        environment = read_environment()
        started_at = datetime.now(UTC)
        provider = SoakProvider()
        summary_provider = SoakSummaryProvider() if context_mode else None
        samples: list[SoakSample] = []
        attempt.phase = "warming"
        with TemporaryDirectory(prefix="harnessix-soak-") as temporary:
            state_root = Path(temporary)
            database = state_root / "session.db"
            wal = state_root / "session.db-wal"
            store = SQLiteSessionStore(database)
            context, compaction = _context_runtime() if context_mode else (None, None)
            async with AgentRuntime(
                store,
                provider,
                context=context,
                compaction=compaction,
                summary_provider=summary_provider,
            ) as runtime:
                before_db, before_wal = file_bytes(database), file_bytes(wal)
                thread = await runtime.create_thread(str(state_root))
                for index in range(warmup_count + turn_count):
                    phase = "warmup" if index < warmup_count else "measure"
                    attempt.phase = "warming" if phase == "warmup" else "measuring"
                    begin = perf_counter_ns()
                    try:
                        async with asyncio.timeout(turn_timeout_seconds):
                            turn = await runtime.run_turn(
                                thread.thread_id,
                                "固定Soak输入",
                                request_id=f"soak-{index}",
                            )
                    except TimeoutError:
                        raise KernelError("soak_turn_timeout", "长会话Soak操作超时") from None
                    elapsed = perf_counter_ns() - begin
                    if turn.status is not TurnStatus.COMPLETED:
                        raise KernelError("soak_turn_failed", "长会话Soak操作未完成")
                    samples.append(
                        latency_sample(
                            run_id, "long_session", len(samples) + 1, phase, "turn_local", elapsed
                        )
                    )

                attempt.phase = "reconciling"
                persisted = await store.get_thread(thread.thread_id)
                events = await store.events(thread.thread_id)
                if (
                    len(persisted.turns) != warmup_count + turn_count
                    or replay(events) != persisted
                    or provider.request_count != warmup_count + turn_count
                ):
                    raise KernelError("soak_replay_mismatch", "长会话Soak持久事实不一致")
                proof = (
                    _context_proof(
                        run_id,
                        persisted,
                        events,
                        warmup_count=warmup_count,
                        summary_request_count=summary_provider.request_count,
                    )
                    if summary_provider is not None
                    else None
                )
                after_db, after_wal = file_bytes(database), file_bytes(wal)

            rss = read_peak_rss()
            samples.append(rss_sample(run_id, "long_session", len(samples) + 1, rss))

        attempt.phase = "publishing"
        run_directory, manifest = publish_measured_run(
            evidence_root,
            run_id=run_id,
            code_revision=code_revision,
            scenario_id="long_session",
            seed=seed,
            environment=environment,
            started_at=started_at,
            load=SoakLoad(
                turn_count=turn_count,
                thread_count=1,
                artifact_count=0,
                warmup_count=warmup_count,
            ),
            samples=tuple(samples),
            provider=provider,
            rss=rss,
            file_watermarks=SoakFileWatermarks(
                db_before_bytes=before_db,
                db_after_bytes=after_db,
                wal_before_bytes=before_wal,
                wal_after_bytes=after_wal,
                artifact_before_bytes=0,
                artifact_after_bytes=0,
            ),
            baseline=turn_count >= 1000 and threshold_profile_ref is None,
            threshold_profile_ref=threshold_profile_ref,
            context_proof=proof,
            summary_request_count=(
                summary_provider.request_count if summary_provider is not None else None
            ),
        )
        attempt.commit(run_directory)
        return run_directory, manifest
