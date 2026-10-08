"""真实 SQLite/MAC 的决定物理回读；声明夹具不代表原 Session 批准或生产 Writer。"""

from dataclasses import replace
from uuid import UUID

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config.git_decision_link_rows import read_git_link_history_rows
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from harnessix.product_config.git_decision_link_wire import encode_product_git_decision_link
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryCoreV2
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_writer import begin_git_prefix_write
from harnessix.product_config.git_prepared_link_rows import (
    prepared_link_columns,
    read_prepared_link_rows,
)
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from tests.delivery.test_git_prefix_ledger import (
    _binding,
    _claim,
    _connection,
    _genesis,
    _publish,
)
from tests.product_config.test_git_decision_link_contracts import case as case
from tests.product_config.test_git_decision_link_sources import fixture
from tests.support.git_delivery_observed_core import model_fields, plan_for, rebind_identity, seal


async def _persist(db, binding, guard, prepared, decision=None, *, mutation=None):
    """仅测试用原物理签发器；它不调用产品决定 Writer，也不认定业务来源。"""
    body = encode_product_git_prepared_link(prepared, checkpoint=lambda: None)
    columns = prepared_link_columns(prepared, body)
    core = prepared.plan.core
    identities = (
        prepared.plan.route.execution.plan_id,
        core.delivery_id,
        core.thread_id,
        core.turn_id,
        core.call.call_id,
    )
    db.execute("BEGIN IMMEDIATE")
    window = begin_git_prefix_write(db, binding.git, binding.git_prefix, checkpoint=lambda: None)
    initial = columns
    if mutation == "initial_columns":
        initial = (*columns[:6], "f" * 64, *columns[7:])
    db.execute("INSERT INTO git_product_links VALUES (?,?,?,?,?,?,?,?,?,?,?)", initial)
    db.execute(
        "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
        (columns[0], 0, "prepared", columns[-1]),
    )
    first = _claim(identities)
    catalog = await _publish(db, binding, guard, window, (first,))
    db.execute("COMMIT")
    if decision is None:
        return catalog
    if mutation == "predecessor_hash":
        decision = decision.model_copy(update={"prepared_body_sha256": "f" * 64})
    text = encode_product_git_decision_link(decision, checkpoint=lambda: None).decode()
    phase = "failed" if mutation == "phase" else decision.phase
    if mutation == "noncanonical":
        text += " "
    db.execute("BEGIN IMMEDIATE")
    window = begin_git_prefix_write(db, binding.git, binding.git_prefix, checkpoint=lambda: None)
    db.execute(
        "UPDATE git_product_links SET phase=?,sequence=1,payload=? WHERE route_id=?",
        (phase, text, columns[0]),
    )
    db.execute("INSERT INTO git_product_link_events VALUES (?,?,?,?)", (columns[0], 1, phase, text))
    stream = next(stream for stream in catalog.streams if stream.first.record_id == identities[0])
    second = _claim(
        identities,
        sequence=2,
        previous=stream.prefix_sha256,
        epoch=first.publication_epoch,
    )
    catalog = await _publish(db, binding, guard, window, (second,))
    db.execute("COMMIT")
    if mutation == "third_event":
        db.execute("BEGIN IMMEDIATE")
        window = begin_git_prefix_write(
            db, binding.git, binding.git_prefix, checkpoint=lambda: None
        )
        db.execute("UPDATE git_product_links SET sequence=2 WHERE route_id=?", (columns[0],))
        db.execute(
            "INSERT INTO git_product_link_events VALUES (?,?,?,?)", (columns[0], 2, phase, text)
        )
        stream = next(
            stream for stream in catalog.streams if stream.first.record_id == identities[0]
        )
        third = _claim(
            identities,
            sequence=3,
            previous=stream.prefix_sha256,
            epoch=first.publication_epoch,
        )
        catalog = await _publish(db, binding, guard, window, (third,))
        db.execute("COMMIT")
    return catalog


def _other_case(case):
    """沿原 Core/Plan 算法创建另一调用的声明，不借用相同 Route 身份。"""
    call = case.core.call.model_copy(update={"call_id": UUID(int=777)})
    core = seal(ProductGitDeliveryCoreV2, {**model_fields(case.core), "call": call})
    core = rebind_identity(core)
    return replace(case, core=core, plan=plan_for(core, case.plan.review_artifact))


@pytest.mark.parametrize("bad_non_target", [False, True])
async def test_mixed_prepared_and_decided_streams_validate_every_association(
    tmp_path, case, bad_non_target
):
    pending = fixture(case, "approved").materials.link
    evidence = fixture(_other_case(case), "denied")
    fact = build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    assert pending.plan.route.execution.plan_id != fact.plan.route.execution.plan_id
    with _binding() as (binding, guard), _connection(tmp_path / "mixed.db") as db:
        await _genesis(db, binding, guard)
        await _persist(db, binding, guard, pending)
        await _persist(
            db,
            binding,
            guard,
            evidence.materials.link,
            fact,
            mutation="predecessor_hash" if bad_non_target else None,
        )
        db.execute("BEGIN")
        before = db.total_changes
        if bad_non_target:
            with pytest.raises(KernelError) as caught:
                read_git_link_history_rows(db, binding, checkpoint=lambda: None)
            assert caught.value.code == "git_decision_link_history_changed"
        else:
            histories, _ = read_git_link_history_rows(db, binding, checkpoint=lambda: None)
            assert len(histories) == 2
            by_route = {h.prepared.plan.route.execution.plan_id: h for h in histories}
            assert by_route[pending.plan.route.execution.plan_id].decision is None
            assert by_route[fact.plan.route.execution.plan_id].decision == fact
        assert db.total_changes == before
        db.execute("ROLLBACK")


@pytest.mark.parametrize("kind", ["pending", "approved", "denied", "cancelled"])
async def test_real_mac_reopen_reads_complete_predecessor_and_decision(tmp_path, case, kind):
    evidence = fixture(case, "approved" if kind == "pending" else kind)
    prepared = evidence.materials.link
    decision = (
        None
        if kind == "pending"
        else build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    )
    path = tmp_path / "links.db"
    with _binding() as (binding, guard):
        with _connection(path) as db:
            await _genesis(db, binding, guard)
            await _persist(db, binding, guard, prepared, decision)
        with _connection(path) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            before = db.total_changes
            histories, rows = read_git_link_history_rows(db, binding, checkpoint=lambda: None)
            assert len(histories) == 1
            assert histories[0].prepared == prepared and histories[0].decision == decision
            assert len(rows.table("git_product_link_events")) == (1 if decision is None else 2)
            assert db.total_changes == before == 0
            assert "request_fingerprint" not in repr(histories[0])
            if kind == "pending":
                assert read_prepared_link_rows(db, binding, checkpoint=lambda: None)[0] == (
                    prepared,
                )
            else:
                with pytest.raises(KernelError):
                    read_prepared_link_rows(db, binding, checkpoint=lambda: None)
            db.execute("ROLLBACK")


@pytest.mark.parametrize(
    "mutation", ["initial_columns", "predecessor_hash", "phase", "noncanonical", "third_event"]
)
async def test_valid_mac_does_not_authorize_wrong_business_projection(tmp_path, case, mutation):
    evidence = fixture(case, "approved")
    decision = build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    with _binding() as (binding, guard), _connection(tmp_path / "bad.db") as db:
        await _genesis(db, binding, guard)
        catalog = await _persist(
            db, binding, guard, evidence.materials.link, decision, mutation=mutation
        )
        db.execute("BEGIN")
        assert (
            read_git_prefix_catalog(
                db, binding.git_verifier, binding.git_prefix_verifier, checkpoint=lambda: None
            )
            == catalog
        )
        before = db.total_changes
        with pytest.raises(KernelError):
            read_git_link_history_rows(db, binding, checkpoint=lambda: None)
        assert db.total_changes == before
        db.execute("ROLLBACK")


async def test_all_original_mac_verified_before_interpreting_any_decision(
    tmp_path, case, monkeypatch
):
    evidence = fixture(case, "approved")
    with _binding() as (binding, guard), _connection(tmp_path / "mac.db") as db:
        await _genesis(db, binding, guard)
        await _persist(
            db,
            binding,
            guard,
            evidence.materials.link,
            build_git_decision_link_sources(evidence, checkpoint=lambda: None),
        )
        db.execute(
            "UPDATE git_record_publications SET seal=zeroblob(length(seal)) WHERE sequence=2"
        )

        def must_not_decode(*args, **kwargs):
            pytest.fail("物理认证失败后不得解释业务决定")

        monkeypatch.setattr(
            "harnessix.product_config.git_decision_link_rows.decode_product_git_prepared_link",
            must_not_decode,
        )
        db.execute("BEGIN")
        with pytest.raises(KernelError) as caught:
            read_git_link_history_rows(db, binding, checkpoint=lambda: None)
        assert caught.value.code == "publication_history_unproven"
        db.execute("ROLLBACK")


@pytest.mark.parametrize("error", [TurnCancelled(), OSError("fixed-control-error")])
async def test_checkpoint_original_error_is_not_replaced(tmp_path, case, error):
    evidence = fixture(case, "approved")
    with _binding() as (binding, guard), _connection(tmp_path / "control.db") as db:
        await _genesis(db, binding, guard)
        await _persist(db, binding, guard, evidence.materials.link)
        db.execute("BEGIN")

        def stop():
            raise error

        with pytest.raises(type(error)) as caught:
            read_git_link_history_rows(db, binding, checkpoint=stop)
        assert caught.value is error
        db.execute("ROLLBACK")
