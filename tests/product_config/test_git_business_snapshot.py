"""Git 业务快照：真实 MAC 行经纯解释形成唯一、只读的 Backup v2 字节。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_business_snapshot import (
    build_git_delivery_business_snapshot,
)
from harnessix.product_config.git_business_snapshot_wire import (
    decode_git_delivery_business_snapshot,
    encode_git_delivery_business_snapshot,
)
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog_and_rows
from tests.delivery.test_git_prefix_ledger import _binding, _connection, _genesis
from tests.product_config.test_git_decision_link_contracts import case as case
from tests.product_config.test_git_decision_link_rows import _persist
from tests.product_config.test_git_decision_link_sources import fixture


async def _snapshot(tmp_path, case, kind: str):
    evidence = fixture(case, "approved" if kind == "pending" else kind)
    decision = (
        None
        if kind == "pending"
        else build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    )
    with _binding() as (binding, guard), _connection(tmp_path / f"{kind}.db") as database:
        await _genesis(database, binding, guard)
        await _persist(database, binding, guard, evidence.materials.link, decision)
        database.execute("BEGIN")
        try:
            catalog, rows = read_git_prefix_catalog_and_rows(
                database,
                binding.git_verifier,
                binding.git_prefix_verifier,
                checkpoint=lambda: None,
            )
            return build_git_delivery_business_snapshot(
                uuid4(), catalog, rows, checkpoint=lambda: None
            )
        finally:
            database.execute("ROLLBACK")


@pytest.mark.parametrize("kind", ["pending", "approved", "denied", "cancelled"])
async def test_real_authenticated_link_history_round_trips_as_canonical_snapshot(
    tmp_path, case, kind: str
) -> None:
    snapshot = await _snapshot(tmp_path, case, kind)

    first = encode_git_delivery_business_snapshot(snapshot, checkpoint=lambda: None)
    reopened = decode_git_delivery_business_snapshot(first, checkpoint=lambda: None)

    assert reopened == snapshot
    assert encode_git_delivery_business_snapshot(reopened, checkpoint=lambda: None) == first
    assert reopened.prefix.revision == (1 if kind == "pending" else 2)
    assert len(reopened.links) == 1
    assert (reopened.links[0].decision is None) is (kind == "pending")


@pytest.mark.parametrize("mutation", ["trailing_space", "duplicate_capture_id"])
async def test_noncanonical_or_ambiguous_snapshot_bytes_are_rejected(
    tmp_path, case, mutation: str
) -> None:
    snapshot = await _snapshot(tmp_path, case, "approved")
    body = encode_git_delivery_business_snapshot(snapshot, checkpoint=lambda: None)
    if mutation == "trailing_space":
        damaged = body + b" "
    else:
        first_field_end = body.index(b',"links"')
        damaged = (
            body[:first_field_end]
            + b',"capture_id":"'
            + str(snapshot.capture_id).encode()
            + b'"'
            + body[first_field_end:]
        )

    with pytest.raises(KernelError) as caught:
        decode_git_delivery_business_snapshot(damaged, checkpoint=lambda: None)

    assert caught.value.code == "git_business_snapshot_invalid"


async def test_capture_identity_is_strictly_typed(tmp_path, case) -> None:
    snapshot = await _snapshot(tmp_path, case, "pending")
    with pytest.raises(KernelError) as caught:
        build_git_delivery_business_snapshot(
            str(snapshot.capture_id),  # type: ignore[arg-type]
            snapshot.prefix,
            # Rows are intentionally unreachable after the capture identity guard.
            object(),  # type: ignore[arg-type]
            checkpoint=lambda: None,
        )

    assert caught.value.code == "git_business_snapshot_invalid"
    assert isinstance(snapshot.capture_id, UUID)


async def test_checkpoint_error_identity_is_preserved(tmp_path, case) -> None:
    snapshot = await _snapshot(tmp_path, case, "pending")
    failure = RuntimeError("fixed-stop")

    def stop() -> None:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        encode_git_delivery_business_snapshot(snapshot, checkpoint=stop)

    assert caught.value is failure


async def test_unsupported_authenticated_scope_has_a_snapshot_specific_error(
    tmp_path, case, monkeypatch
) -> None:
    from harnessix.product_config import git_business_snapshot

    snapshot = await _snapshot(tmp_path, case, "pending")

    def reject(*args, **kwargs):
        raise KernelError("git_decision_link_scope_unsupported", "fixed")

    monkeypatch.setattr(git_business_snapshot, "interpret_git_link_histories", reject)
    with pytest.raises(KernelError) as caught:
        build_git_delivery_business_snapshot(
            snapshot.capture_id,
            snapshot.prefix,
            object(),  # type: ignore[arg-type]
            checkpoint=lambda: None,
        )

    assert caught.value.code == "git_business_snapshot_scope_unsupported"
