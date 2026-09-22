"""通过真实Agent Runtime执行长会话，并发布可独立重算的低敏Soak证据。"""

from __future__ import annotations

import asyncio
import re
import subprocess
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
from scripts.soak_evidence import publish_run, read_published_run
from scripts.soak_manifest import (
    SoakFaultCounts,
    SoakFileWatermarks,
    SoakLoad,
    SoakManifest,
    SoakProviderEvidence,
    SoakRssEvidence,
)
from scripts.soak_provider import SoakProvider
from scripts.soak_rss import read_peak_rss
from scripts.soak_sample_file import SAMPLE_FILENAME, sample_sha256
from scripts.soak_samples import SoakSample, validate_sample_series


def _file_bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _check_release_revision(code_revision: str) -> None:
    """正式基线只允许绑定当前干净Git Revision，不能伪造来源。"""

    repository = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=normal"),
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise KernelError("soak_revision_invalid", "正式Soak无法核对源码Revision") from None
    if head != code_revision or dirty:
        raise KernelError("soak_revision_invalid", "正式Soak源码Revision不一致或工作区未清洁")


def _latency_sample(run_id: str, index: int, phase: str, elapsed_ns: int) -> SoakSample:
    return SoakSample(
        spec_version="harnessix.soak-sample/v1",
        run_id=run_id,
        scenario_id="long_session",
        sample_index=index,
        phase=phase,
        metric="turn_local",
        value=elapsed_ns,
        unit="ns",
        clock="monotonic_ns",
    )


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
        _check_release_revision(code_revision)

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
            before_db, before_wal = _file_bytes(database), _file_bytes(wal)
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
                samples.append(_latency_sample(run_id, len(samples) + 1, phase, elapsed))

            persisted = await store.get_thread(thread.thread_id)
            if (
                len(persisted.turns) != warmup_count + turn_count
                or replay(await store.events(thread.thread_id)) != persisted
                or provider.request_count != warmup_count + turn_count
            ):
                raise KernelError("soak_replay_mismatch", "长会话Soak持久事实不一致")
            after_db, after_wal = _file_bytes(database), _file_bytes(wal)

        rss = read_peak_rss()
        samples.append(
            SoakSample(
                spec_version="harnessix.soak-sample/v1",
                run_id=run_id,
                scenario_id="long_session",
                sample_index=len(samples) + 1,
                phase="measure",
                metric="rss_peak",
                value=rss.rss_bytes,
                unit="bytes",
                rss_source=rss.source,
                rss_raw_unit=rss.raw_unit,
                rss_normalization=rss.normalization,
                rss_raw_value=rss.raw_value,
                rss_bytes=rss.rss_bytes,
            )
        )

    frozen_samples = tuple(samples)
    counts = {"turn_local": turn_count, "rss_peak": 1}
    manifest = SoakManifest(
        spec_version="harnessix.soak-manifest/v1",
        run_id=run_id,
        code_revision=code_revision,
        scenario_version="harnessix.soak-scenario/v1",
        scenario_id="long_session",
        measurement_boundary="core_runtime",
        seed=seed,
        provider=SoakProviderEvidence(
            mode="deterministic_stateless_v1",
            script_version=SoakProvider.SCRIPT_VERSION,
            request_count=provider.request_count,
        ),
        platform=environment.platform,
        python_version=environment.python_version,
        cpu_count=environment.cpu_count,
        physical_memory_bytes=environment.physical_memory_bytes,
        hardware_class=environment.hardware_class,
        started_at=started_at,
        ended_at=datetime.now(UTC),
        status="baseline" if turn_count >= 1000 else "unverified",
        load=SoakLoad(
            turn_count=turn_count,
            thread_count=1,
            artifact_count=0,
            warmup_count=warmup_count,
        ),
        sample_counts=counts,
        quantile_method="nearest_rank_v1",
        statistics=validate_sample_series(
            frozen_samples,
            run_id=run_id,
            scenario_id="long_session",
            expected_measured=counts,
        ),
        rss=SoakRssEvidence(
            source=rss.source,
            raw_unit=rss.raw_unit,
            normalization=rss.normalization,
            peak_bytes=rss.rss_bytes,
            unit_verified=rss.unit_verified,
        ),
        file_watermarks=SoakFileWatermarks(
            db_before_bytes=before_db,
            db_after_bytes=after_db,
            wal_before_bytes=before_wal,
            wal_after_bytes=after_wal,
            artifact_before_bytes=0,
            artifact_after_bytes=0,
        ),
        fault_counts=SoakFaultCounts(
            cancelled=0,
            timed_out=0,
            eof=0,
            unknown_effect=0,
            duplicate_effect=0,
            orphan=0,
        ),
        evidence_sha256={SAMPLE_FILENAME: sample_sha256(frozen_samples)},
        threshold_profile_ref=None,
    )
    run_directory, _ = publish_run(evidence_root, manifest, frozen_samples)
    restored, _ = read_published_run(run_directory)
    if restored != manifest:
        raise KernelError("soak_run_invalid", "长会话Soak发布复核失败")
    return run_directory, manifest
