"""通过真实Agent Runtime执行长会话，并发布可独立重算的低敏Soak证据。"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.session.sqlite import SQLiteSessionStore
from scripts.soak_environment import read_environment
from scripts.soak_manifest import SoakFileWatermarks, SoakLoad, SoakManifest
from scripts.soak_provider import SoakProvider
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
    """使用临时Session完成固定负载；仅正式样本进入统计和Run发布。"""

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
    ):
        raise KernelError("soak_load_invalid", "长会话Soak负载参数无效")
    if turn_count >= 1000:
        check_release_revision(code_revision)

    run_id = uuid4().hex
    environment = read_environment()
    started_at = datetime.now(UTC)
    provider = SoakProvider()
    samples: list[SoakSample] = []
    with TemporaryDirectory(prefix="harnessix-soak-") as temporary:
        state_root = Path(temporary)
        database = state_root / "session.db"
        wal = state_root / "session.db-wal"
        store = SQLiteSessionStore(database)
        async with AgentRuntime(store, provider) as runtime:
            before_db, before_wal = file_bytes(database), file_bytes(wal)
            thread = await runtime.create_thread(str(state_root))
            for index in range(warmup_count + turn_count):
                phase = "warmup" if index < warmup_count else "measure"
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

            persisted = await store.get_thread(thread.thread_id)
            if (
                len(persisted.turns) != warmup_count + turn_count
                or replay(await store.events(thread.thread_id)) != persisted
                or provider.request_count != warmup_count + turn_count
            ):
                raise KernelError("soak_replay_mismatch", "长会话Soak持久事实不一致")
            after_db, after_wal = file_bytes(database), file_bytes(wal)

        rss = read_peak_rss()
        samples.append(rss_sample(run_id, "long_session", len(samples) + 1, rss))

    return publish_measured_run(
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
        baseline=turn_count >= 1000,
    )
