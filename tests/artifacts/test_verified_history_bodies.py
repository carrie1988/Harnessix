"""历史Artifact的正文在同一进程里只完整核对一次（Beta Issue #8，ADR 0158）。

此前每次请求模型之前都把历史里每份正文重新扫描一遍（公开输出的密钥扫描约11 MB/s），
耗时随会话里累计的命令输出线性增长；0.10.0b24上一个57步的会话因此超过5秒时限而中断。之后的步骤仍逐项核对记录归属、
来源认证、清单、是否过期与正文SHA-256，只跳过对同一份正文、同一保护范围结果不会变的检查。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from harnessix.agent.errors import KernelError
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.batch_verify import verify_references
from harnessix.artifacts.contracts import ArtifactRef, HistoryReferenceCheck
from tests.artifacts.helpers import exercise, results


def _entries(thread, turn) -> tuple[HistoryReferenceCheck, ...]:
    return tuple(
        HistoryReferenceCheck(
            owner_thread_id=thread.thread_id,
            call_id=result.call_id,
            reference=ArtifactRef.model_validate_json(json.dumps(result.output["artifact"])),
            purpose="tool_result",
            omitted_field=None,
        )
        for result in results(turn)
    )


def _count_scans(artifacts, monkeypatch) -> list[bytes]:
    scanned: list[bytes] = []
    original = artifacts._publication.check_body

    async def counting(body: bytes, **kwargs) -> None:
        scanned.append(body)
        await original(body, **kwargs)

    monkeypatch.setattr(artifacts._publication, "check_body", counting)
    return scanned


async def test_each_body_is_scanned_once_and_later_steps_only_check_its_digest(
    tmp_path, monkeypatch
) -> None:
    store, _, scope, thread, turn = await exercise(tmp_path, count=300)
    entries = _entries(thread, turn)
    # 准备数据的那一轮已经核对过；用一个新的存储对象，从第一次核对开始数。
    artifacts = artifact_sqlite.SQLiteArtifactStore(store)
    scanned = _count_scans(artifacts, monkeypatch)

    await verify_references(artifacts, entries, workspace_scope=scope)
    assert len(scanned) == len(entries) == 1
    for _ in range(3):
        await verify_references(artifacts, entries, workspace_scope=scope)
    assert len(scanned) == 1


@pytest.mark.parametrize("change", ["body", "missing", "expired", "scope"])
async def test_later_steps_still_find_a_changed_missing_or_expired_body(
    tmp_path, monkeypatch, change
) -> None:
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=300)
    entries = _entries(thread, turn)
    await verify_references(artifacts, entries, workspace_scope=scope)

    if change == "body":
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE agent_artifacts SET body = zeroblob(size_bytes)")
    elif change == "missing":
        with sqlite3.connect(store.path) as db:
            db.execute("DELETE FROM agent_artifacts")
    elif change == "expired":
        expires = entries[0].reference.expires_at
        later = expires + timedelta(seconds=1)
        monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: later)
    with pytest.raises(KernelError) as error:
        await verify_references(
            artifacts, entries, workspace_scope="0" * 64 if change == "scope" else scope
        )
    assert (
        error.value.code
        == {
            "body": "artifact_corrupt",
            "missing": "artifact_not_found",
            "expired": "artifact_expired",
            "scope": "artifact_not_found",
        }[change]
    )


async def test_a_new_store_scans_again(tmp_path, monkeypatch) -> None:
    """记住的结果属于一个存储对象（与它的公开输出保护同生命周期），不跨进程或跨存储沿用。"""
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=300)
    entries = _entries(thread, turn)
    await verify_references(artifacts, entries, workspace_scope=scope)

    reopened = artifact_sqlite.SQLiteArtifactStore(store)
    scanned = _count_scans(reopened, monkeypatch)
    await verify_references(reopened, entries, workspace_scope=scope)
    assert len(scanned) == 1
