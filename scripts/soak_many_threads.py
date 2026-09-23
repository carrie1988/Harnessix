"""经真实应用服务列表与Runtime重启测量多Thread规模。"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.service import AgentApplicationService
from harnessix.protocol.contracts import ThreadListParams
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.sqlite import SQLiteSessionStore
from scripts.soak_attempt import attempt_scope
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


async def _list_all(
    service: AgentApplicationService,
    *,
    run_id: str,
    expected_ids: frozenset[str],
    limit: int,
    phase: str,
    timeout_seconds: float,
    samples: list[SoakSample],
) -> None:
    """验证完整游标覆盖后才接受一次列表遍历。"""

    seen: set[str] = set()
    cursor: str | None = None
    max_pages = (len(expected_ids) + limit - 1) // limit + 1
    for _ in range(max_pages):
        begin = perf_counter_ns()
        page_task = asyncio.create_task(
            service.list_threads(ThreadListParams(cursor=cursor, limit=limit))
        )
        try:
            page = await asyncio.wait_for(asyncio.shield(page_task), timeout_seconds)
        except TimeoutError:
            # SQLite异步调用须先自然收敛，再删除Windows临时数据库。
            await asyncio.gather(page_task, return_exceptions=True)
            raise KernelError("soak_list_timeout", "多Thread列表操作超时") from None
        elapsed = perf_counter_ns() - begin
        ids = [str(thread.thread_id) for thread in page.threads]
        if not ids or len(ids) > limit or any(value in seen for value in ids):
            raise KernelError("soak_list_invalid", "多Thread列表分页重复或为空")
        seen.update(ids)
        samples.append(
            latency_sample(
                run_id,
                "many_threads",
                len(samples) + 1,
                phase,
                "thread_list_page",
                elapsed,
            )
        )
        cursor = page.next_cursor
        if cursor is None:
            if seen != expected_ids:
                raise KernelError("soak_list_invalid", "多Thread列表分页未覆盖全部Thread")
            return
    raise KernelError("soak_list_invalid", "多Thread列表分页未收敛")


async def _restart_and_list(
    store: SQLiteSessionStore,
    provider: SoakProvider,
    workspace: Path,
    *,
    run_id: str,
    expected_ids: frozenset[str],
    limit: int,
    phase: str,
    startup_timeout_seconds: float,
    page_timeout_seconds: float,
    samples: list[SoakSample],
) -> None:
    """逐次新建Runtime/Service，启动与分页均使用同一持久Session。"""

    runtime = AgentRuntime(store, provider)
    begin = perf_counter_ns()
    startup_task = asyncio.create_task(runtime.__aenter__())
    try:
        await asyncio.wait_for(asyncio.shield(startup_task), startup_timeout_seconds)
    except TimeoutError:
        outcome = await asyncio.gather(startup_task, return_exceptions=True)
        if not isinstance(outcome[0], BaseException):
            await runtime.__aexit__(None, None, None)
        raise KernelError("soak_startup_timeout", "多Thread启动恢复超时") from None
    try:
        service = AgentApplicationService(
            runtime,
            store,
            SQLiteProtocolRequestStore(store.path),
            workspace=workspace,
        )
        elapsed = perf_counter_ns() - begin
        samples.append(
            latency_sample(
                run_id,
                "many_threads",
                len(samples) + 1,
                phase,
                "app_service_startup",
                elapsed,
            )
        )
        try:
            await _list_all(
                service,
                run_id=run_id,
                expected_ids=expected_ids,
                limit=limit,
                phase=phase,
                timeout_seconds=page_timeout_seconds,
                samples=samples,
            )
        finally:
            await service.close()
    finally:
        await runtime.__aexit__(None, None, None)


async def run_many_threads(
    evidence_root: Path,
    *,
    code_revision: str,
    thread_count: int,
    list_limit: int = 50,
    restart_count: int = 3,
    seed: int = 0,
    startup_timeout_seconds: float = 30.0,
    page_timeout_seconds: float = 30.0,
) -> tuple[Path, SoakManifest]:
    """填充独立Session，预热一次并测量多次真实启动与完整分页。"""

    if (
        not isinstance(code_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None
        or not isinstance(evidence_root, Path)
        or type(thread_count) is not int
        or not 1 <= thread_count <= 5_000
        or type(list_limit) is not int
        or not 1 <= list_limit <= 200
        or type(restart_count) is not int
        or not 1 <= restart_count <= 10
        or type(seed) is not int
        or seed < 0
        or not 0 < startup_timeout_seconds <= 300
        or not 0 < page_timeout_seconds <= 300
        or (thread_count >= 500 and restart_count < 3)
    ):
        raise KernelError("soak_load_invalid", "多Thread Soak负载参数无效")
    if thread_count >= 500:
        check_release_revision(code_revision)

    run_id = uuid4().hex
    with attempt_scope(
        evidence_root, run_id=run_id, code_revision=code_revision, scenario_id="many_threads"
    ) as attempt:
        environment = read_environment()
        started_at = datetime.now(UTC)
        provider = SoakProvider()
        samples: list[SoakSample] = []
        attempt.phase = "warming"
        with TemporaryDirectory(prefix="harnessix-soak-") as temporary:
            workspace = Path(temporary)
            database = workspace / "session.db"
            wal = workspace / "session.db-wal"
            store = SQLiteSessionStore(database)
            async with AgentRuntime(store, provider) as runtime:
                before_db, before_wal = file_bytes(database), file_bytes(wal)
                expected_ids: set[str] = set()
                for _ in range(thread_count):
                    thread = await runtime.create_thread(str(workspace))
                    expected_ids.add(str(thread.thread_id))
                if {str(identity) for identity in await store.thread_ids()} != expected_ids:
                    raise KernelError("soak_thread_count_invalid", "多Thread持久数量不符")

            for index in range(restart_count + 1):
                attempt.phase = "warming" if index == 0 else "measuring"
                await _restart_and_list(
                    store,
                    provider,
                    workspace,
                    run_id=run_id,
                    expected_ids=frozenset(expected_ids),
                    limit=list_limit,
                    phase="warmup" if index == 0 else "measure",
                    startup_timeout_seconds=startup_timeout_seconds,
                    page_timeout_seconds=page_timeout_seconds,
                    samples=samples,
                )
                if index == 0:
                    warmup_count = len(samples)

            attempt.phase = "reconciling"
            if {
                str(identity) for identity in await store.thread_ids()
            } != expected_ids or provider.request_count != 0:
                raise KernelError("soak_thread_count_invalid", "多Thread恢复后持久数量不符")
            after_db, after_wal = file_bytes(database), file_bytes(wal)
            rss = read_peak_rss()
            samples.append(rss_sample(run_id, "many_threads", len(samples) + 1, rss))

        attempt.phase = "publishing"
        run_directory, manifest = publish_measured_run(
            evidence_root,
            run_id=run_id,
            code_revision=code_revision,
            scenario_id="many_threads",
            seed=seed,
            environment=environment,
            started_at=started_at,
            load=SoakLoad(
                turn_count=0,
                thread_count=thread_count,
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
            baseline=thread_count >= 500,
        )
        attempt.commit(run_directory)
        return run_directory, manifest
