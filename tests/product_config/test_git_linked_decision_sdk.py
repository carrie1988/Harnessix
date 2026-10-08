"""原 SDK 决定的实际只读消费；测试签发仅造输入，不冒充已交付的生产 Writer。"""

from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.product_config.git_approval_history_projection import interpret_git_approval_history
from harnessix.product_config.git_approval_history_proof import ApprovalHistoryEvidence
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from harnessix.product_config.git_decision_link_wire import encode_product_git_decision_link
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prefix_writer import (
    begin_git_prefix_write,
    publish_git_prefix_changes,
)
from harnessix.product_config.git_prepared_link_proof import PreparedLinkEvidence
from tests.delivery.test_git_prefix_ledger import _claim
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_prepared_approval_history import _decide, _prepared, _reader
from tests.product_config.test_git_prepared_link_ledger import _case, _database, _history, _rows


async def _decision_input(actual, prepared):
    """只组装测试正文；生产 Reader 必须重新完整读取所有来源，不能消费此 Evidence。"""
    history = await _history(actual)
    router = actual.scenario.router
    route_id = prepared.plan.route.execution.plan_id
    route, events, approval = (
        router.status(route_id),
        router.events(route_id),
        router.approval(route_id),
    )
    projection = interpret_git_approval_history(
        history, prepared, route, events, approval, checkpoint=lambda: None
    )
    evidence = ApprovalHistoryEvidence(
        PreparedLinkEvidence(prepared, history, route, b""), projection, events, approval, None
    )
    return build_git_decision_link_sources(evidence, checkpoint=lambda: None)


async def _persist_test_decision(actual, fact):
    """原固定测试资源中追加带原 MAC 的声明，用于证明 MAC 不等同原业务决定。"""
    publication = actual.scenario.session._publication
    route_id = fact.plan.route.execution.plan_id
    body = encode_product_git_decision_link(fact, checkpoint=lambda: None).decode()
    async with _database(actual) as db:
        with git_prefix_sql_window(db, checkpoint=lambda: None):
            db.execute("BEGIN IMMEDIATE")
            catalog = read_git_prefix_catalog(
                db,
                publication.git_verifier,
                publication.git_prefix_verifier,
                checkpoint=lambda: None,
            )
            stream = next(
                stream for stream in catalog.streams if stream.first.record_id == route_id
            )
            window = begin_git_prefix_write(
                db, publication.git, publication.git_prefix, checkpoint=lambda: None
            )
            db.execute(
                "UPDATE git_product_links SET phase=?,sequence=1,payload=? WHERE route_id=?",
                (fact.phase, body, str(route_id)),
            )
            db.execute(
                "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
                (str(route_id), 1, fact.phase, body),
            )
            core = fact.plan.core
            claim = _claim(
                (route_id, core.delivery_id, core.thread_id, core.turn_id, core.call.call_id),
                sequence=2,
                previous=stream.prefix_sha256,
                epoch=stream.first.publication_epoch,
            )
            await publish_git_prefix_changes(
                window,
                (claim,),
                publication.git,
                publication.git_prefix,
                publication._events._protection,
                cancel=CancelToken(),
            )
            db.execute("COMMIT")
        return _rows(db)


@pytest.mark.parametrize(
    "kind", ["approved", "denied", "cancelled", "missing", "bad-non-target-ref"]
)
async def test_actual_sdk_reopen_original_decision_sources_without_writes(
    tmp_path, config, monkeypatch, kind
):
    async def inspect(actual):
        scenario = actual.scenario
        prepared, sealed = await _prepared(actual)
        if kind == "cancelled":
            await scenario.client.cancel_turn(
                actual.thread.thread_id, actual.turn.turn_id, request_id="linked-cancel"
            )
        else:
            await _decide(actual, monkeypatch, "rejected" if kind == "denied" else "approved")
        fact = await _decision_input(actual, prepared)
        route = fact.plan.route.execution.plan_id
        if kind == "bad-non-target-ref":
            fact = fact.model_copy(
                update={"request_event": fact.request_event.model_copy(update={"digest": "f" * 64})}
            )
            requested = UUID(int=route.int ^ 1)
        else:
            requested = route
        if kind != "missing":
            sealed = await _persist_test_decision(actual, fact)
        history, source, stores = (
            await _history(actual),
            _source_snapshot(scenario.root),
            scenario.unchanged_state(),
        )
        async with _database(actual, read_only=True) as db:
            db.execute("BEGIN")
            reader = _reader(actual, db)
            if kind in {"missing", "bad-non-target-ref"}:
                with pytest.raises(KernelError) as caught:
                    await reader.read_linked_decision(
                        requested, cancel=CancelToken(), checkpoint=lambda: None
                    )
                assert caught.value.code == (
                    "git_decision_link_missing"
                    if kind == "missing"
                    else "git_decision_link_history_changed"
                )
            else:
                result = await reader.read_linked_decision(
                    requested, cancel=CancelToken(), checkpoint=lambda: None
                )
                assert result == fact and result is not fact
            assert db.total_changes == 0 and _rows(db) == sealed
        assert await _history(actual) == history
        assert _source_snapshot(scenario.root) == source and scenario.unchanged_state() == stores

    await _case(tmp_path, config, monkeypatch, inspect)
