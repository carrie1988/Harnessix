"""环境纠正后的唯一实际 SDK：一遍批准回读，随后非目标原 MAC 行损坏拒绝。"""

import json
from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from tests.product_config.conftest import product_config
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_prepared_approval_history import _decide, _prepared, _reader
from tests.product_config.test_git_prepared_link_ledger import _case, _database, _history, _rows


@pytest.mark.asyncio
async def test_corrected_one_approved_read_then_non_target_mac_rejected(tmp_path, monkeypatch):
    receipt = {
        "fixture_runs": 1,
        "approved_reads": 0,
        "real_model_requests": 0,
        "turn_timeout_unchanged": True,
        "writer_proof_issued": False,
    }

    async def check(actual):
        prepared, original_rows = await _prepared(actual)
        await _decide(actual, monkeypatch, "approved")
        route = prepared.plan.route.execution.plan_id
        history = await _history(actual)
        source = _source_snapshot(actual.scenario.root)
        unchanged = actual.scenario.unchanged_state()
        async with _database(actual, read_only=True) as db:
            db.execute("BEGIN")
            result = await _reader(actual, db).read_decided(
                route, cancel=CancelToken(), checkpoint=lambda: None
            )
            assert result.fact_kind == "approved" and result.plan == prepared.plan
            assert result.request_event.digest == next(
                ref.body_sha256
                for ref in history.body_refs
                if ref.event_id == result.request_event.event_id
            )
            assert db.total_changes == 0 and _rows(db) == original_rows
            receipt.update(approved_reads=1, approved_read_pass=True, positive_reader_writes=0)
        assert await _history(actual) == history
        assert _source_snapshot(actual.scenario.root) == source
        assert actual.scenario.unchanged_state() == unchanged
        async with _database(actual) as fault:
            original = fault.execute("SELECT seal FROM git_record_publications").fetchall()
            assert len(original) == 1 and type(original[0][0]) is bytes
            seal = original[0][0]
            corrupt = bytes([seal[0] ^ 1]) + seal[1:]
            cursor = fault.execute("UPDATE git_record_publications SET seal=?", (corrupt,))
            assert cursor.rowcount == 1
            receipt["deliberate_fault_writes"] = fault.total_changes
        async with _database(actual, read_only=True) as db:
            db.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                await _reader(actual, db).read_decided(
                    UUID(int=route.int ^ 1), cancel=CancelToken(), checkpoint=lambda: None
                )
            assert caught.value.code == "publication_history_unproven" and db.total_changes == 0
            receipt.update(
                non_target_original_mac_corruption_rejected=True,
                negative_error_code=caught.value.code,
                negative_reader_writes=0,
            )
        assert await _history(actual) == history
        assert _source_snapshot(actual.scenario.root) == source
        assert actual.scenario.unchanged_state() == unchanged
        receipt.update(session_history_unchanged=True, workspace_unchanged=True)
        (tmp_path / "sdk-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        )

    await _case(tmp_path, product_config(), monkeypatch, check)
