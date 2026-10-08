"""返回对象的同步末端绑定；资源替身只证明接线，不证明原 MAC 或 SDK。"""

from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_approval_history as subject
from harnessix.product_config import git_prepared_link_ledger as ledger
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from tests.product_config.test_git_decision_link_contracts import case as case
from tests.product_config.test_git_decision_link_sources import fixture


@pytest.mark.parametrize("kind", ["approved", "denied", "cancelled"])
@pytest.mark.parametrize(
    "mutation", [None, "valid_digest", "foreign_equality", "valid_copy_redirection"]
)
async def test_last_callback_return_binding(case, tmp_path, monkeypatch, kind, mutation):
    evidence = fixture(case, kind)
    route_id = evidence.materials.link.plan.route.execution.plan_id
    baseline = build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    seen, foreign_calls = [], []

    class Foreign:
        def __eq__(self, other):
            foreign_calls.append(other)
            return True

    @contextmanager
    def unchanged(*args):
        yield lambda: None

    monkeypatch.setattr(ledger, "require_git_review_host", lambda *args: lambda: None)
    # 仅隔离来源依赖以验证返回绑定；此替身不是原连接或 Runtime 锁的认证正控。
    monkeypatch.setattr(ledger, "_prepared_git_connection_observer", lambda *args: lambda: None)
    monkeypatch.setattr(
        ledger, "_prepared_git_connection_lifecycle_observer", lambda *args: lambda: None
    )
    monkeypatch.setattr(ledger, "_prepared_runtime_thread_observer", lambda *args: lambda: None)
    monkeypatch.setattr(ledger, "observe_prepared_state", unchanged)

    async def read_all(resources, cancel, budget, check, read_set):
        read_set.approvals[route_id] = evidence
        read_set.evidence[route_id] = evidence.materials
        return ()

    monkeypatch.setattr(subject, "_read_all", read_all)
    monkeypatch.setattr(subject._ApprovalReadSet, "require_sql", lambda self, db, check: check())

    def terminal(self, *args):
        assert self.approvals[route_id] is evidence
        assert build_git_decision_link_sources(evidence, checkpoint=lambda: None) == baseline
        seen.append("evidence-terminal-unchanged")
        args[-1]()

    monkeypatch.setattr(subject._ApprovalReadSet, "terminal", terminal)

    def callback():
        frame, frames = inspect.currentframe(), []
        try:
            while frame is not None:
                frames.append(frame)
                frame = frame.f_back
            if mutation and any(f.f_code.co_name == "git_prefix_sql_window" for f in frames):
                for current in frames:
                    if current.f_code.co_name == "read_decided" and "result" in current.f_locals:
                        if mutation == "valid_copy_redirection":
                            original = current.f_locals["result"]
                            # 校验副本必须只能交付该实际核验的新快照，不得仍返回旧别名。
                            current.f_locals["read_set"].declaration = (
                                route_id,
                                original.model_copy(deep=True),
                            )
                        value = "f" * 64 if mutation == "valid_digest" else Foreign()
                        if mutation == "valid_copy_redirection":
                            value = "f" * 64
                        object.__setattr__(
                            current.f_locals["result"], "prepared_body_sha256", value
                        )
                        seen.append("last-external-return-only-mutation")
        finally:
            del frame
            frames.clear()

    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("BEGIN")
        reader = subject.ProductGitPreparedApprovalHistoryReader(
            db,
            object(),
            object(),
            SimpleNamespace(session=SimpleNamespace(path=tmp_path / "sessions.db")),
            object(),
            snapshot_ports=object(),
            workspace_scope="1" * 64,
        )
        if mutation in {"valid_digest", "foreign_equality"}:
            with pytest.raises(KernelError) as caught:
                await reader.read_decided(route_id, cancel=CancelToken(), checkpoint=callback)
            assert caught.value.code == (
                "git_decision_source_changed"
                if mutation == "valid_digest"
                else "git_delivery_plan_invalid"
            )
        else:
            actual = await reader.read_decided(route_id, cancel=CancelToken(), checkpoint=callback)
            assert actual == baseline
        assert db.in_transaction and db.total_changes == 0
    assert foreign_calls == []
    assert seen == (
        ["last-external-return-only-mutation", "evidence-terminal-unchanged"]
        if mutation
        else ["evidence-terminal-unchanged"]
    )
