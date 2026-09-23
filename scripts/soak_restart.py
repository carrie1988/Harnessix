"""对完整产品组合根执行可复核的跨进程重启Soak。"""

from __future__ import annotations

import asyncio
import math
import re
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_contracts import ProductActionStartupRecoveryReport
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.codec import canonical_product_config_bytes
from harnessix.product_config.contracts import (
    EnvironmentSecretSourceConfig,
    ModelCapabilities,
    ModelProfile,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
)
from harnessix.sdk.agent_client import AgentClient, AgentSDKError
from harnessix.sdk.subprocess import SubprocessAgentTransport
from harnessix.trusted_actions.recovery_contracts import ActionRecoveryScanReport
from scripts.soak_attempt import attempt_scope
from scripts.soak_environment import read_environment
from scripts.soak_manifest import (
    SoakFaultCounts,
    SoakFileWatermarks,
    SoakLoad,
    SoakManifestV5,
    SoakProfileReference,
)
from scripts.soak_restart_proof import (
    PRODUCT_DB_FILES,
    SoakRestartChildResult,
    SoakRestartCycle,
    SoakRestartProof,
)
from scripts.soak_rss import RssObservation, read_peak_rss
from scripts.soak_run_common import (
    check_release_revision,
    file_bytes,
    latency_sample,
    publish_measured_run,
    rss_sample,
)
from scripts.soak_samples import SoakSample


def _write_offline_config(path: Path) -> None:
    """Provider只使用包装器注入的假Secret和保留.test域名。"""

    secret = SecretReference(name="soak-key", version="v1")
    config = ProductConfigV2(
        active_profile="soak",
        secret_sources=(
            EnvironmentSecretSourceConfig(secret=secret, environment_variable="HARNESSIX_SOAK_KEY"),
        ),
        providers=(
            ProviderDefinition(
                provider_id="soak",
                kind="openai_chat",
                base_url="https://api.openai.test/v1",
                credential=secret,
            ),
        ),
        profiles=(
            ModelProfile(
                profile_id="soak",
                provider_id="soak",
                model="soak-offline",
                capabilities=ModelCapabilities(),
            ),
        ),
    )
    path.write_bytes(canonical_product_config_bytes(config))
    path.chmod(0o600)


def _new_client(
    config: Path, workspace: Path, state: Path, gate: Path
) -> tuple[AgentClient, SubprocessAgentTransport]:
    gate.mkdir(mode=0o700, exist_ok=False)
    command = (
        sys.executable,
        str(Path(__file__).with_name("soak_restart_child.py")),
        str(config),
        str(workspace),
        str(state),
        str(gate),
    )
    transport = SubprocessAgentTransport(command)
    return AgentClient(transport), transport


def _identity_digest(identities: set[UUID]) -> str:
    """只在内存中保留Thread ID，公开证明仅含稳定集合摘要。"""

    return sha256("".join(f"{item}\n" for item in sorted(identities)).encode()).hexdigest()


async def _create_threads(client: AgentClient, workspace: Path, thread_count: int) -> set[UUID]:
    identities: set[UUID] = set()
    for ordinal in range(1, thread_count + 1):
        thread = await client.create_thread(
            str(workspace), request_id=f"soak-restart-thread-{ordinal}"
        )
        if thread.thread_id in identities:
            raise KernelError("soak_restart_thread_invalid", "产品创建了重复Thread")
        identities.add(thread.thread_id)
    return identities


async def _list_exact(client: AgentClient, expected: set[UUID]) -> None:
    """全页核对集合而非只核对页数或汇总计数。"""

    observed: set[UUID] = set()
    cursor: str | None = None
    cursors: set[str] = set()
    max_pages = (len(expected) + 49) // 50 + 1
    for _ in range(max_pages):
        page = await client.list_threads(cursor=cursor, limit=50)
        for thread in page.threads:
            if thread.thread_id in observed:
                raise KernelError("soak_restart_thread_invalid", "产品列表出现重复Thread")
            observed.add(thread.thread_id)
        cursor = page.next_cursor
        if cursor is None:
            if observed != expected:
                raise KernelError("soak_restart_thread_invalid", "产品列表Thread集合不一致")
            return
        if cursor in cursors:
            raise KernelError("soak_restart_thread_invalid", "产品列表Cursor重复")
        cursors.add(cursor)
    raise KernelError("soak_restart_thread_invalid", "产品列表超过固定分页上限")


def _recovery(
    state: Path, ordinal: int
) -> tuple[ActionRecoveryScanReport, ProductActionStartupRecoveryReport]:
    """子进程退出后从持久产品配置库重读扫描和恢复报告。"""

    if not (state / "product-config.db").is_file():
        raise KernelError("soak_restart_recovery_invalid", "产品恢复配置库不存在")
    try:
        with SQLiteProductRuntimeConfigStore(state / "product-config.db") as store:
            scans = store.action_recovery_scans()
            reports = store.action_recovery_reports()
    except (OSError, KernelError):
        raise KernelError("soak_restart_recovery_invalid", "产品恢复报告无法读取") from None
    if len(scans) != ordinal or len(reports) != ordinal or scans[-1].owner_generation != ordinal:
        raise KernelError("soak_restart_recovery_invalid", "产品恢复报告或Owner代际缺失")
    return scans[-1], reports[-1]


def _normal_child_rss(gate: Path) -> RssObservation:
    target = gate / "child-result.json"
    try:
        if target.stat().st_size > 4096 or (gate / "child-failure.txt").exists():
            raise OSError
        result = SoakRestartChildResult.model_validate_json(target.read_bytes())
    except (OSError, ValueError):
        raise KernelError("soak_restart_child_invalid", "正常产品子进程缺少RSS结果") from None
    return result.rss


async def _wait_ack(gate: Path) -> None:
    while not (gate / "crash.ack").is_file():  # noqa: ASYNC110, ASYNC240
        await asyncio.sleep(0.005)
    if (gate / "crash.ack").read_bytes() != b"ACK\n":
        raise KernelError("soak_restart_crash_invalid", "受控硬退出ACK无效")


def _file_watermarks(
    state: Path, *, require_complete: bool = False
) -> tuple[dict[str, int], dict[str, int]]:
    actual = {path.relative_to(state).as_posix() for path in state.rglob("*.db")}
    if actual - PRODUCT_DB_FILES or (require_complete and actual != PRODUCT_DB_FILES):
        raise KernelError("soak_restart_state_invalid", "产品SQLite文件集合不完整")
    db = {name: file_bytes(state / name) for name in sorted(PRODUCT_DB_FILES)}
    wal = {name: file_bytes(state / f"{name}-wal") for name in sorted(PRODUCT_DB_FILES)}
    return db, wal


async def _cycle(
    *,
    config: Path,
    workspace: Path,
    state: Path,
    gate: Path,
    expected: set[UUID] | None,
    thread_count: int,
    ordinal: int,
    phase: str,
    timeout_seconds: float,
    run_id: str,
    sample_index: int,
) -> tuple[SoakRestartCycle, SoakSample | None, set[UUID], RssObservation | None]:
    client, transport = _new_client(config, workspace, state, gate)
    started = perf_counter_ns()
    stage = "startup"
    try:
        async with asyncio.timeout(timeout_seconds):
            await client.initialize()
        elapsed = perf_counter_ns() - started
        if expected is None:
            stage = "thread"
            async with asyncio.timeout(timeout_seconds):
                expected = await _create_threads(client, workspace, thread_count)
        stage = "thread"
        async with asyncio.timeout(timeout_seconds):
            await _list_exact(client, expected)
        if phase == "crash":
            stage = "crash"
            (gate / "crash.request").write_bytes(b"CRASH\n")
            try:
                async with asyncio.timeout(timeout_seconds):
                    await _wait_ack(gate)
                    await client.list_threads(limit=1)
            except AgentSDKError as error:
                if error.code != "server_closed":
                    raise KernelError(
                        "soak_restart_crash_invalid", "子进程EOF错误码不匹配"
                    ) from None
            else:
                raise KernelError("soak_restart_crash_invalid", "受控硬退出后未观察到stdio EOF")
        sample = (
            None
            if phase == "crash"
            else latency_sample(
                run_id,
                "restart",
                sample_index,
                "warmup" if phase == "warmup" else "measure",
                "product_startup",
                elapsed,
            )
        )
    except TimeoutError:
        raise KernelError("soak_restart_timeout", "产品重启阶段超时") from None
    except AgentSDKError:
        code = (
            "soak_restart_startup_failed"
            if stage == "startup"
            else "soak_restart_crash_invalid"
            if stage == "crash"
            else "soak_restart_thread_invalid"
        )
        raise KernelError(code, "产品子进程协议阶段失败") from None
    finally:
        await client.close()
    if transport.snapshot().state != "closed":
        raise KernelError("soak_restart_close_invalid", "产品子进程关闭未收敛")
    rss = None if phase == "crash" else _normal_child_rss(gate)
    if phase == "crash" and (gate / "child-result.json").exists():
        raise KernelError("soak_restart_crash_invalid", "受控硬退出却写入正常结果")
    scan, report = _recovery(state, ordinal)
    cycle = SoakRestartCycle(
        ordinal=ordinal,
        phase=phase,
        startup_sample_index=None if sample is None else sample.sample_index,
        thread_count=len(expected),
        thread_set_sha256=_identity_digest(expected),
        owner_generation=scan.owner_generation,
        recovery_scan=scan,
        recovery_report=report,
        close_state="hard_exit" if phase == "crash" else "closed",
    )
    return cycle, sample, expected, rss


async def run_product_restart(
    evidence_root: Path,
    *,
    code_revision: str,
    thread_count: int,
    warmup_count: int,
    measured_restarts: int,
    timeout_seconds: float = 30.0,
    threshold_profile_ref: SoakProfileReference | None = None,
) -> tuple[Path, SoakManifestV5]:
    """以一次预热、一次硬退出和至少三次独立新进程启动发布V5证据。"""

    if (
        not isinstance(evidence_root, Path)
        or not isinstance(code_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None
        or type(thread_count) is not int
        or not 1 <= thread_count <= 10_000
        or type(warmup_count) is not int
        or warmup_count != 1
        or type(measured_restarts) is not int
        or not 3 <= measured_restarts <= 20
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 20 <= timeout_seconds <= 120
        or (threshold_profile_ref is not None and thread_count < 500)
    ):
        raise KernelError("soak_load_invalid", "产品重启Soak负载参数无效")
    formal = thread_count >= 500
    if formal:
        check_release_revision(code_revision)
    run_id = uuid4().hex
    with attempt_scope(
        evidence_root,
        run_id=run_id,
        code_revision=code_revision,
        scenario_id="restart",
        threshold_profile_ref=threshold_profile_ref,
    ) as attempt:
        environment = read_environment()
        started_at = datetime.now(UTC)
        samples: list[SoakSample] = []
        cycles: list[SoakRestartCycle] = []
        child_rss: list[RssObservation] = []
        attempt.phase = "warming"
        with TemporaryDirectory(prefix="harnessix-restart-soak-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            state = root / "state"
            state.mkdir(mode=0o700)
            config = root / "config.json"
            _write_offline_config(config)
            db_before, wal_before = _file_watermarks(state)
            expected: set[UUID] | None = None
            for ordinal in range(1, measured_restarts + 3):
                phase = "warmup" if ordinal == 1 else "crash" if ordinal == 2 else "measure"
                attempt.phase = "warming" if phase != "measure" else "measuring"
                gate = root / f"gate-{ordinal}"
                cycle, sample, expected, rss = await _cycle(
                    config=config,
                    workspace=workspace,
                    state=state,
                    gate=gate,
                    expected=expected,
                    thread_count=thread_count,
                    ordinal=ordinal,
                    phase=phase,
                    timeout_seconds=timeout_seconds,
                    run_id=run_id,
                    sample_index=len(samples) + 1,
                )
                cycles.append(cycle)
                if sample is not None:
                    samples.append(sample)
                if rss is not None:
                    child_rss.append(rss)
            attempt.phase = "reconciling"
            db_after, wal_after = _file_watermarks(state, require_complete=True)
        runner_rss = read_peak_rss()
        if not child_rss or any(
            (item.source, item.raw_unit, item.normalization)
            != (runner_rss.source, runner_rss.raw_unit, runner_rss.normalization)
            for item in child_rss
        ):
            raise KernelError("soak_restart_rss_invalid", "产品重启RSS来源或单位不一致")
        server_rss = max(child_rss, key=lambda item: item.rss_bytes)
        peak_rss = max((runner_rss, server_rss), key=lambda item: item.rss_bytes)
        samples.append(rss_sample(run_id, "restart", len(samples) + 1, peak_rss))
        assert expected is not None
        proof = SoakRestartProof(
            spec_version="harnessix.soak-restart-proof/v1",
            run_id=run_id,
            thread_count=thread_count,
            thread_set_sha256=_identity_digest(expected),
            cycles=tuple(cycles),
            hard_exit_ack=True,
            hard_exit_eof=True,
            runner_rss_bytes=runner_rss.rss_bytes,
            server_rss_bytes=server_rss.rss_bytes,
            db_before_bytes_by_name=db_before,
            db_after_bytes_by_name=db_after,
            wal_before_bytes_by_name=wal_before,
            wal_after_bytes_by_name=wal_after,
        )
        watermarks = SoakFileWatermarks(
            db_before_bytes=sum(db_before.values()),
            db_after_bytes=sum(db_after.values()),
            wal_before_bytes=sum(wal_before.values()),
            wal_after_bytes=sum(wal_after.values()),
            artifact_before_bytes=0,
            artifact_after_bytes=0,
        )
        run_directory, manifest = publish_measured_run(
            evidence_root,
            run_id=run_id,
            code_revision=code_revision,
            scenario_id="restart",
            seed=0,
            environment=environment,
            started_at=started_at,
            load=SoakLoad(
                turn_count=0,
                thread_count=thread_count,
                artifact_count=0,
                pending_limit=None,
                warmup_count=1,
                fault_matrix_version="product-restart-v1",
            ),
            samples=tuple(samples),
            provider=None,
            restart_proof=proof,
            rss=peak_rss,
            file_watermarks=watermarks,
            baseline=formal and threshold_profile_ref is None,
            threshold_profile_ref=threshold_profile_ref,
            fault_counts=SoakFaultCounts(
                cancelled=0,
                timed_out=0,
                eof=1,
                unknown_effect=0,
                duplicate_effect=0,
                orphan=0,
            ),
        )
        assert isinstance(manifest, SoakManifestV5)
        attempt.commit(run_directory)
        return run_directory, manifest
