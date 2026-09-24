"""通过真实Agent、Coding Tool和Artifact Store运行混合大小件Soak。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from typing import Literal
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, ToolResultContent, Turn, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, ArtifactRef, ArtifactToolResult
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from scripts.soak_artifact_proof import (
    SoakArtifactCleanup,
    SoakArtifactEntry,
    SoakArtifactProof,
)
from scripts.soak_attempt import attempt_scope
from scripts.soak_environment import read_environment
from scripts.soak_manifest import (
    SoakFileWatermarks,
    SoakLoad,
    SoakManifestV3,
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

_SMALL_LINES = 3
_NEAR_LINES = 2500
_LINE = "needle" + "x" * 200 + "\n"


class SoakArtifactProvider(SoakProvider):
    """按当前Turn的受信夹具选择输出Tool Call，不保留请求历史。"""

    __slots__ = ("target",)

    def __init__(self) -> None:
        super().__init__()
        self.target = "small.txt"

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        if request.step not in (1, 2):
            raise KernelError("soak_provider_invalid", "Artifact固定脚本步骤无效")
        self.request_count += 1
        cancel.checkpoint()
        yield ResponseStarted(response_id="soak-artifact")
        if request.step == 1:
            if not any(tool.name == "grep" for tool in request.tools):
                raise KernelError("soak_provider_invalid", "Artifact场景缺少受信grep工具")
            cancel.checkpoint()
            yield ToolCallCompleted(
                call_id="soak-artifact-call",
                tool="grep",
                arguments={"query": "needle", "include": self.target, "max_results": 1},
            )
            cancel.checkpoint()
            yield ResponseCompleted(finish_reason="tool_calls")
            return
        cancel.checkpoint()
        yield TextStarted(content_id="soak-artifact-answer")
        cancel.checkpoint()
        yield TextCompleted(content_id="soak-artifact-answer", text="soak-complete")
        cancel.checkpoint()
        yield ResponseCompleted()


class MeasuredArtifactStore(SQLiteArtifactStore):
    """仅记录真实发布方法的单调时钟耗时，不修改事务。"""

    def __init__(self, session: SQLiteSessionStore) -> None:
        super().__init__(session)
        self.publish_ns: list[int] = []

    async def publish(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        output: ArtifactToolResult,
        *,
        expected_sequence: int,
        max_output_chars: int,
    ) -> Thread:
        start = perf_counter_ns()
        result = await super().publish(
            thread_id,
            turn_id,
            call,
            output,
            expected_sequence=expected_sequence,
            max_output_chars=max_output_chars,
        )
        self.publish_ns.append(perf_counter_ns() - start)
        return result


def _body_counts(database: Path) -> tuple[int, int, int]:
    """只读查询临时业务库的逻辑正文、清理墓碑与Manifest数量。"""

    # sqlite3.Connection的with只提交/回滚，不关闭句柄；Windows临时库必须显式关闭。
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT COALESCE(SUM(length(body)), 0), "
            "COALESCE(SUM(state = 'expired'), 0), COUNT(*) FROM agent_artifacts"
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


def _artifact_result(turn: Turn) -> ArtifactRef:
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    if (
        len(results) != 1
        or results[0].outcome != "succeeded"
        or not isinstance(results[0].output, dict)
        or not isinstance(results[0].output.get("artifact"), dict)
    ):
        raise KernelError("soak_artifact_invalid", "Artifact Turn未完成唯一发布")
    try:
        return ArtifactRef.model_validate_json(json.dumps(results[0].output["artifact"]))
    except (TypeError, ValueError, ValidationError):
        raise KernelError("soak_artifact_invalid", "Artifact引用合同无效") from None


async def run_artifact_growth(
    evidence_root: Path,
    *,
    code_revision: str,
    turn_count: int,
    warmup_count: int,
    seed: int = 0,
    turn_timeout_seconds: float = 60.0,
    page_timeout_seconds: float = 15.0,
    threshold_profile_ref: SoakProfileReference | None = None,
) -> tuple[Path, SoakManifestV3]:
    """单Thread每Turn发布一件，完整分页并到期清理后发布v3证据。"""

    if (
        not isinstance(evidence_root, Path)
        or not isinstance(code_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None
        or type(turn_count) is not int
        or not 1 <= turn_count <= 500
        or type(warmup_count) is not int
        or not 0 <= warmup_count <= 2
        or type(seed) is not int
        or not 0 <= seed <= 100_000
        or not 0 < turn_timeout_seconds <= 300
        or not 0 < page_timeout_seconds <= 120
        or (turn_count >= 20 and warmup_count != 2)
        or (threshold_profile_ref is not None and turn_count < 20)
    ):
        raise KernelError("soak_load_invalid", "Artifact Soak负载参数无效")
    if turn_count >= 20:
        check_release_revision(code_revision)

    run_id = uuid4().hex
    total = warmup_count + turn_count
    with attempt_scope(
        evidence_root,
        run_id=run_id,
        code_revision=code_revision,
        scenario_id="artifact_growth",
        threshold_profile_ref=threshold_profile_ref,
    ) as attempt:
        environment = read_environment()
        started_at = datetime.now(UTC)
        provider = SoakArtifactProvider()
        samples: list[SoakSample] = []
        entries: list[SoakArtifactEntry] = []
        references: list[ArtifactRef] = []
        attempt.phase = "warming"
        with TemporaryDirectory(prefix="harnessix-artifact-soak-") as temporary:
            state_root = Path(temporary)
            workspace = state_root / "repo"
            workspace.mkdir()
            (workspace / "small.txt").write_text(_LINE * _SMALL_LINES, encoding="utf-8")
            (workspace / "near.txt").write_text(_LINE * _NEAR_LINES, encoding="utf-8")
            database = state_root / "session.db"
            wal = state_root / "session.db-wal"
            session = SQLiteSessionStore(database)
            artifacts = MeasuredArtifactStore(session)
            async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
                async with AgentRuntime(
                    session, provider, scoped_tools=tools, artifacts=artifacts
                ) as runtime:
                    before_db, before_wal = file_bytes(database), file_bytes(wal)
                    thread = await runtime.create_thread(str(tools.workspace_root))
                    for index in range(total):
                        phase: Literal["warmup", "measure"] = (
                            "warmup" if index < warmup_count else "measure"
                        )
                        attempt.phase = "warming" if phase == "warmup" else "measuring"
                        measured_index = index - warmup_count
                        near = phase == "measure" and (measured_index + seed) % 4 == 0
                        provider.target = "near.txt" if near else "small.txt"
                        before_publishes = len(artifacts.publish_ns)
                        try:
                            async with asyncio.timeout(turn_timeout_seconds):
                                turn = await runtime.run_turn(
                                    thread.thread_id,
                                    "固定Artifact Soak输入",
                                    request_id=f"artifact-{index}",
                                )
                        except TimeoutError:
                            raise KernelError(
                                "soak_artifact_timeout", "Artifact Turn超时"
                            ) from None
                        if turn.status is not TurnStatus.COMPLETED or (
                            len(artifacts.publish_ns) != before_publishes + 1
                        ):
                            raise KernelError(
                                "soak_artifact_invalid", "Artifact发布数量或Turn状态无效"
                            )
                        ref = _artifact_result(turn)
                        if not ref.complete or (
                            near
                            and not MAX_ARTIFACT_BYTES * 8 // 10
                            <= ref.size_bytes
                            < MAX_ARTIFACT_BYTES
                        ):
                            raise KernelError(
                                "soak_artifact_size_invalid", "Artifact大小或完整性无效"
                            )
                        if not near and ref.size_bytes >= MAX_ARTIFACT_BYTES * 8 // 10:
                            raise KernelError("soak_artifact_size_invalid", "小件Artifact大小无效")
                        references.append(ref)
                        samples.append(
                            latency_sample(
                                run_id,
                                "artifact_growth",
                                len(samples) + 1,
                                phase,
                                "artifact_publish",
                                artifacts.publish_ns[-1],
                            )
                        )
                        publish_index = len(samples)
                        offset, pages, read_records = 0, [], 0
                        while True:
                            start = perf_counter_ns()
                            page_task = asyncio.create_task(
                                artifacts.read(
                                    thread.thread_id,
                                    tools.workspace_scope,
                                    ref.artifact_id,
                                    offset=offset,
                                    limit=200,
                                )
                            )
                            try:
                                page = await asyncio.wait_for(
                                    asyncio.shield(page_task), page_timeout_seconds
                                )
                            except TimeoutError:
                                # 先排空在途SQLite任务，再退出临时业务库作用域。
                                await asyncio.gather(page_task, return_exceptions=True)
                                raise KernelError(
                                    "soak_artifact_timeout", "Artifact分页超时"
                                ) from None
                            elapsed = perf_counter_ns() - start
                            count = page.text.count("\n")
                            if page.artifact != ref or page.offset != offset or count <= 0:
                                raise KernelError(
                                    "soak_artifact_page_invalid", "Artifact分页不连续"
                                )
                            read_records += count
                            samples.append(
                                latency_sample(
                                    run_id,
                                    "artifact_growth",
                                    len(samples) + 1,
                                    phase,
                                    "artifact_read",
                                    elapsed,
                                )
                            )
                            pages.append(len(samples))
                            if page.next_offset is None:
                                break
                            if page.next_offset <= offset or page.next_offset > ref.records:
                                raise KernelError(
                                    "soak_artifact_page_invalid", "Artifact分页游标无效"
                                )
                            offset = page.next_offset
                        if read_records != ref.records:
                            raise KernelError(
                                "soak_artifact_page_invalid", "Artifact分页记录未覆盖"
                            )
                        entries.append(
                            SoakArtifactEntry(
                                ordinal=index + 1,
                                phase=phase,
                                size_class="near_limit" if near else "small",
                                size_bytes=ref.size_bytes,
                                record_count=ref.records,
                                read_record_count=read_records,
                                page_count=len(pages),
                                publish_sample_index=publish_index,
                                read_sample_indices=tuple(pages),
                            )
                        )

                    attempt.phase = "reconciling"
                    persisted = await session.get_thread(thread.thread_id)
                    events = await session.events(thread.thread_id)
                    if (
                        len(persisted.turns) != total
                        or replay(events) != persisted
                        or provider.request_count != total * 2
                    ):
                        raise KernelError(
                            "soak_replay_mismatch", "Artifact持久状态或模型请求数不一致"
                        )
                    before_body, tombstones, manifests = _body_counts(database)
                    if before_body != sum(ref.size_bytes for ref in references) or tombstones:
                        raise KernelError("soak_artifact_cleanup_invalid", "清理前Artifact数量无效")
                    future = max(ref.expires_at for ref in references) + timedelta(seconds=1)
                    with patch("harnessix.artifacts.sqlite.utc_now", return_value=future):
                        report = await artifacts.collect(limit=100)
                        if report.expired != total or report.protected or report.next_after:
                            raise KernelError(
                                "soak_artifact_cleanup_invalid", "Artifact到期清理不完整"
                            )
                        for ref in references:
                            try:
                                await artifacts.read(
                                    thread.thread_id, tools.workspace_scope, ref.artifact_id
                                )
                            except KernelError as error:
                                if error.code != "artifact_expired":
                                    raise KernelError(
                                        "soak_artifact_cleanup_invalid", "过期Artifact拒绝语义无效"
                                    ) from None
                            else:
                                raise KernelError(
                                    "soak_artifact_cleanup_invalid", "过期Artifact仍可读取"
                                )
                    after_body, tombstones, manifests_after = _body_counts(database)
                    if after_body or tombstones != total or manifests_after != manifests:
                        raise KernelError(
                            "soak_artifact_cleanup_invalid", "Artifact墓碑或正文数量无效"
                        )
                    after_db, after_wal = file_bytes(database), file_bytes(wal)

            rss = read_peak_rss()
            samples.append(rss_sample(run_id, "artifact_growth", len(samples) + 1, rss))

        proof = SoakArtifactProof(
            spec_version="harnessix.soak-artifact-proof/v1",
            run_id=run_id,
            entries=tuple(entries),
            cleanup=SoakArtifactCleanup(
                before_body_bytes=before_body,
                after_body_bytes=after_body,
                expired_count=total,
                tombstone_count=tombstones,
                manifest_count=manifests_after,
                protected_count=0,
            ),
        )
        attempt.phase = "publishing"
        run_directory, manifest = publish_measured_run(
            evidence_root,
            run_id=run_id,
            code_revision=code_revision,
            scenario_id="artifact_growth",
            seed=seed,
            environment=environment,
            started_at=started_at,
            load=SoakLoad(
                turn_count=total,
                thread_count=1,
                artifact_count=total,
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
                artifact_after_bytes=before_body,
            ),
            baseline=turn_count >= 20 and threshold_profile_ref is None,
            threshold_profile_ref=threshold_profile_ref,
            artifact_proof=proof,
        )
        if not isinstance(manifest, SoakManifestV3):
            raise KernelError("soak_run_invalid", "Artifact Run证据版本无效")
        attempt.commit(run_directory)
        return run_directory, manifest
