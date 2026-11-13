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
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.batch_verify import verify_references
from harnessix.artifacts.contracts import ArtifactRef, HistoryReferenceCheck
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import exercise, results, step


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
        if body:  # 每批开头用空正文确认保护作用域仍然有效，不算扫描正文
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


class _ClosableProtection:
    """可关闭的公开输出保护：关闭后与真实作用域一样拒绝检查。"""

    def __init__(self) -> None:
        self.closed = False

    def _ensure_open(self) -> None:
        if self.closed:
            # 与真实的Secret保护作用域关闭后相同。
            raise KernelError("trusted_action_secret_unavailable", "保护作用域已关闭")

    def assert_public_json(self, value, *, checkpoint) -> None:  # type: ignore[no-untyped-def]
        self._ensure_open()

    def assert_public_jsonl(self, body: bytes, *, checkpoint) -> None:  # type: ignore[no-untyped-def]
        self._ensure_open()


async def test_remembered_results_are_not_used_after_the_protection_scope_closes(
    tmp_path,
) -> None:
    """作用域关闭后完整核对失败即拒绝；记住的结果不能绕过这一点（发布会话审阅意见）。"""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文\n" * 300, encoding="utf-8")
    store = SQLiteSessionStore(tmp_path / "session.db")
    protection = _ClosableProtection()
    artifacts = artifact_sqlite.SQLiteArtifactStore(store, public_output_protection=protection)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        provider = ScriptedProvider([step(), answer()])
        async with AgentRuntime(
            store, provider, scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "归档搜索", request_id="r")
        scope = tools.workspace_scope
    entries = _entries(thread, turn)
    await verify_references(artifacts, entries, workspace_scope=scope)

    protection.closed = True
    with pytest.raises(KernelError) as cached:
        await verify_references(artifacts, entries, workspace_scope=scope)
    fresh = artifact_sqlite.SQLiteArtifactStore(store, public_output_protection=protection)
    with pytest.raises(KernelError) as full:
        await verify_references(fresh, entries, workspace_scope=scope)
    # 命中记住结果的核对与首次完整核对一样失败，错误码相同。
    assert cached.value.code == full.value.code == "public_output_secret_unavailable"
