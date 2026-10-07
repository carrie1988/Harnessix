"""原实际 SDK 审批证据的内部声明消费；不发布 Git 决定或执行效果。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.product_config import git_prepared_approval_history as history_module
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_prepared_approval_history import _decide, _prepared, _reader
from tests.product_config.test_git_prepared_link_ledger import _case, _database, _history, _rows


@pytest.mark.asyncio
async def test_original_sdk_approved_evidence_maps_under_original_read_control(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        prepared, git_rows = await _prepared(actual)
        await _decide(actual, monkeypatch, "approved")
        original_history = await _history(actual)
        source = _source_snapshot(actual.scenario.root)
        unchanged = actual.scenario.unchanged_state()
        original_read = history_module.read_original_approval_evidence
        declarations = []

        async def consume(*args, **kwargs):
            # 原函数完成完整资源读取；映射借同一个实际父控制，非独立空检查点。
            evidence = await original_read(*args, **kwargs)
            declaration = build_git_decision_link_sources(evidence, checkpoint=kwargs["checkpoint"])
            assert declaration.request_event.digest == next(
                ref.body_sha256
                for ref in evidence.materials.history.body_refs
                if ref.event_id == declaration.request_event.event_id
            )
            declarations.append(declaration)
            return evidence

        monkeypatch.setattr(history_module, "read_original_approval_evidence", consume)
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            result = await _reader(actual, database).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            )
            assert len(result) == len(declarations) == 1
            assert declarations[0].fact_kind == "approved"
            assert declarations[0].plan == prepared.plan
            assert result[0].linkage_state == "decision_not_linked"
            assert database.total_changes == 0
            assert _rows(database) == git_rows
        assert _source_snapshot(actual.scenario.root) == source
        assert await _history(actual) == original_history
        assert actual.scenario.unchanged_state() == unchanged

    await _case(tmp_path, config, monkeypatch, inspect)
