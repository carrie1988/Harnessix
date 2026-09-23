"""通过真实Agent SDK、子进程stdio和App Server测量容量与迟到响应。"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.sdk.agent_client import AgentClient
from harnessix.sdk.subprocess import SubprocessAgentTransport, SubprocessTransportSnapshot
from scripts.soak_attempt import attempt_scope
from scripts.soak_environment import read_environment
from scripts.soak_manifest import (
    SoakFaultCounts,
    SoakFileWatermarks,
    SoakLoad,
    SoakManifestV4,
    SoakProfileReference,
)
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
from scripts.soak_sdk_proof import SoakSdkChildResult, SoakSdkProof, SoakSdkRound


async def _wait_marker(path: Path, timeout_seconds: float) -> None:
    """门闩标记来自私有临时目录；不以固定sleep代替子进程确认。"""

    async with asyncio.timeout(timeout_seconds):
        while not path.is_file():  # noqa: ASYNC110, ASYNC240
            await asyncio.sleep(0.005)


def _snapshot(
    transport: SubprocessAgentTransport, *, pending: int, abandoned: int
) -> SubprocessTransportSnapshot:
    state = transport.snapshot()
    if (
        state.state != "running"
        or state.pending_requests != pending
        or state.abandoned_requests != abandoned
        or state.failure_code is not None
    ):
        raise KernelError("soak_sdk_capacity_invalid", "SDK容量或迟到响应状态无效")
    return state


def _child_result(gate: Path) -> SoakSdkChildResult:
    result_path = gate / "child-result.json"
    if not result_path.is_file():
        failure_path = gate / "child-failure.txt"
        reason = "unknown"
        if failure_path.is_file() and failure_path.stat().st_size <= 128:
            candidate = failure_path.read_bytes()
            if re.fullmatch(rb"[A-Za-z][A-Za-z0-9_]*:[a-z0-9_]{1,64}\n", candidate):
                reason = candidate.decode("ascii").strip()
        raise KernelError("soak_sdk_child_failed", f"SDK子进程未生成结果：{reason}")
    return SoakSdkChildResult.model_validate_json(result_path.read_bytes())


async def _round(
    client: AgentClient,
    transport: SubprocessAgentTransport,
    gate: Path,
    ordinal: int,
    phase: str,
    timeout_seconds: float,
) -> SoakSdkRound:
    """满容量后只放行被取消请求，确认迟到Response释放墓碑而非普通Response。"""

    limit = transport.max_pending_requests
    index = ordinal - 1
    normal = [asyncio.create_task(client.list_threads(limit=1)) for _ in range(limit - 1)]
    cancelled = asyncio.create_task(client.list_threads(limit=2))
    overflow: asyncio.Task[object] | None = None
    try:
        await _wait_marker(gate / f"ready-{index}", timeout_seconds)
        at_capacity = _snapshot(transport, pending=limit, abandoned=0)
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        after_cancel = _snapshot(transport, pending=limit - 1, abandoned=1)
        overflow = asyncio.create_task(client.list_threads(limit=3))
        if (gate / f"overflow-{index}").exists():
            raise KernelError("soak_sdk_capacity_invalid", "溢出请求提前进入Server")
        await asyncio.to_thread((gate / f"release-cancelled-{index}").touch)
        await _wait_marker(gate / f"overflow-{index}", timeout_seconds)
        after_late = _snapshot(transport, pending=limit, abandoned=0)
        await asyncio.to_thread((gate / f"release-rest-{index}").touch)
        async with asyncio.timeout(timeout_seconds):
            responses = await asyncio.gather(*normal, overflow)
        if any(response.threads or response.next_cursor for response in responses):
            raise KernelError("soak_sdk_response_invalid", "SDK列表Response业务结果无效")
        after_drain = _snapshot(transport, pending=0, abandoned=0)
        return SoakSdkRound(
            ordinal=ordinal,
            phase=phase,
            pending_at_capacity=at_capacity.pending_requests,
            pending_after_cancel=after_cancel.pending_requests,
            abandoned_after_cancel=after_cancel.abandoned_requests,
            pending_after_late=after_late.pending_requests,
            abandoned_after_late=after_late.abandoned_requests,
            pending_after_drain=after_drain.pending_requests,
            abandoned_after_drain=after_drain.abandoned_requests,
            overflow_entered_after_late=True,
            successful_responses=len(responses),
        )
    except TimeoutError:
        raise KernelError("soak_sdk_timeout", "SDK容量阶段等待超时") from None
    finally:
        # 故障时先放开子进程门闩，随后由唯一Transport Close回收所有在途请求。
        await asyncio.to_thread((gate / f"release-cancelled-{index}").touch)
        await asyncio.to_thread((gate / f"release-rest-{index}").touch)
        tasks = (*normal, cancelled, *((overflow,) if overflow is not None else ()))
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_sdk_capacity(
    evidence_root: Path,
    *,
    code_revision: str,
    measured_rounds: int,
    warmup_count: int,
    roundtrip_count: int,
    pending_limit: int = 64,
    seed: int = 0,
    timeout_seconds: float = 30.0,
    threshold_profile_ref: SoakProfileReference | None = None,
) -> tuple[Path, SoakManifestV4]:
    """正式规模需64容量、1轮预热、至少3轮测量及20次正常往返。"""

    if (
        not isinstance(evidence_root, Path)
        or not isinstance(code_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None
        or type(measured_rounds) is not int
        or not 1 <= measured_rounds <= 20
        or type(warmup_count) is not int
        or not 0 <= warmup_count <= 1
        or type(roundtrip_count) is not int
        or not 1 <= roundtrip_count <= 1000
        or type(pending_limit) is not int
        or not 2 <= pending_limit <= 64
        or type(seed) is not int
        or not 0 <= seed <= 100_000
        or not 0 < timeout_seconds <= 120
        or (
            (measured_rounds >= 3 or threshold_profile_ref is not None)
            and (pending_limit != 64 or warmup_count != 1 or roundtrip_count < 20)
        )
        or (threshold_profile_ref is not None and measured_rounds < 3)
    ):
        raise KernelError("soak_load_invalid", "SDK容量Soak负载参数无效")
    formal = measured_rounds >= 3
    if formal:
        check_release_revision(code_revision)

    run_id = uuid4().hex
    with attempt_scope(
        evidence_root,
        run_id=run_id,
        code_revision=code_revision,
        scenario_id="sdk_capacity",
        threshold_profile_ref=threshold_profile_ref,
    ) as attempt:
        environment = read_environment()
        started_at = datetime.now(UTC)
        samples: list[SoakSample] = []
        rounds: list[SoakSdkRound] = []
        attempt.phase = "warming"
        with TemporaryDirectory(prefix="harnessix-sdk-soak-") as temporary:
            root = Path(temporary)
            gate = root / "gate"
            gate.mkdir()
            database = root / "session.db"
            wal = root / "session.db-wal"
            before_db, before_wal = file_bytes(database), file_bytes(wal)
            transport = SubprocessAgentTransport(
                (
                    sys.executable,
                    str(Path(__file__).with_name("soak_sdk_child.py")),
                    str(database),
                    str(gate),
                    str(pending_limit),
                ),
                max_pending_requests=pending_limit,
            )
            client = AgentClient(transport)
            try:
                async with asyncio.timeout(timeout_seconds):
                    initialized = await client.initialize()
                if initialized.limits.max_pending_requests != pending_limit:
                    raise KernelError("soak_sdk_limit_invalid", "SDK协商容量与传输容量不匹配")
                for ordinal in range(1, warmup_count + measured_rounds + 1):
                    phase = "warmup" if ordinal <= warmup_count else "measure"
                    attempt.phase = "warming" if phase == "warmup" else "measuring"
                    rounds.append(
                        await _round(client, transport, gate, ordinal, phase, timeout_seconds)
                    )
                roundtrip_window_start = perf_counter_ns()
                for _ in range(roundtrip_count):
                    start = perf_counter_ns()
                    async with asyncio.timeout(timeout_seconds):
                        result = await client.list_threads(limit=50)
                    if result.threads or result.next_cursor:
                        raise KernelError("soak_sdk_response_invalid", "SDK正常往返结果无效")
                    samples.append(
                        latency_sample(
                            run_id,
                            "sdk_capacity",
                            len(samples) + 1,
                            "measure",
                            "sdk_roundtrip",
                            perf_counter_ns() - start,
                        )
                    )
                roundtrip_window_ns = perf_counter_ns() - roundtrip_window_start
            except TimeoutError:
                raise KernelError("soak_sdk_timeout", "SDK正常往返或握手超时") from None
            finally:
                await client.close()
            final = transport.snapshot()
            if final.state != "closed" or final.pending_requests or final.abandoned_requests:
                raise KernelError("soak_sdk_close_invalid", "SDK子进程关闭后仍有未决容量")
            child_result = _child_result(gate)
            attempt.phase = "reconciling"
            client_rss = read_peak_rss()
            server_rss = child_result.rss
            if (
                client_rss.source != server_rss.source
                or client_rss.raw_unit != server_rss.raw_unit
                or client_rss.normalization != server_rss.normalization
            ):
                raise KernelError("soak_sdk_rss_invalid", "SDK双进程RSS单位不一致")
            rss = max((client_rss, server_rss), key=lambda item: item.rss_bytes)
            samples.append(rss_sample(run_id, "sdk_capacity", len(samples) + 1, rss))
            after_db, after_wal = file_bytes(database), file_bytes(wal)

        proof = SoakSdkProof(
            spec_version="harnessix.soak-sdk-proof/v1",
            run_id=run_id,
            negotiated_pending_limit=pending_limit,
            rounds=tuple(rounds),
            roundtrip_sample_indices=tuple(
                item.sample_index for item in samples if item.metric == "sdk_roundtrip"
            ),
            roundtrip_window_ns=roundtrip_window_ns,
            client_rss_bytes=client_rss.rss_bytes,
            server_rss_bytes=server_rss.rss_bytes,
            close_state="closed",
            pending_after_close=final.pending_requests,
            abandoned_after_close=final.abandoned_requests,
        )
        attempt.phase = "publishing"
        run_directory, manifest = publish_measured_run(
            evidence_root,
            run_id=run_id,
            code_revision=code_revision,
            scenario_id="sdk_capacity",
            seed=seed,
            environment=environment,
            started_at=started_at,
            load=SoakLoad(
                turn_count=0,
                thread_count=0,
                artifact_count=0,
                pending_limit=pending_limit,
                warmup_count=warmup_count,
                fault_matrix_version="sdk-capacity-v1",
            ),
            samples=tuple(samples),
            provider=SoakProvider(),
            rss=rss,
            file_watermarks=SoakFileWatermarks(
                db_before_bytes=before_db,
                db_after_bytes=after_db,
                wal_before_bytes=before_wal,
                wal_after_bytes=after_wal,
                artifact_before_bytes=0,
                artifact_after_bytes=0,
            ),
            fault_counts=SoakFaultCounts(
                cancelled=len(rounds),
                timed_out=0,
                eof=0,
                unknown_effect=0,
                duplicate_effect=0,
                orphan=0,
            ),
            sdk_proof=proof,
            baseline=formal and threshold_profile_ref is None,
            threshold_profile_ref=threshold_profile_ref,
        )
        assert isinstance(manifest, SoakManifestV4)
        attempt.commit(run_directory)
        return run_directory, manifest
