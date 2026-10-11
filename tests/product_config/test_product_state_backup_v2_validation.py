"""Backup v2候选GitDB：只读核对完整原认证前缀及Manifest剖面。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store_genesis import initialize_git_store_v2
from harnessix.product_config.git_prefix_catalog import encode_git_prefix_catalog
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prefix_writer import (
    begin_git_prefix_write,
    initialize_git_prefix_genesis,
    publish_git_prefix_changes,
)
from harnessix.product_config.session_key_store import load_session_key
from harnessix.product_config.state_backup_files import PrivateStateTree
from harnessix.product_config.state_backup_v2_contracts import (
    GIT_DATABASE,
    ProductStateGitBackupProfile,
)
from harnessix.product_config.state_backup_v2_validation import validate_git_backup_profile
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from harnessix.session.maintenance_io import MaintenanceIOControl
from harnessix.session.store_publication import SessionPublicationBinding


class _Protection:
    def publication_context(self) -> dict[str, object]:
        return {"scope_id": str(UUID(int=0)), "bindings": []}

    def assert_public_json(
        self, value: JsonValue, *, checkpoint: Callable[[], None]
    ) -> None:
        checkpoint()

    def assert_public_jsonl(self, body: bytes, *, checkpoint: Callable[[], None]) -> None:
        checkpoint()


@dataclass(frozen=True, slots=True)
class _Candidate:
    root: Path
    store_id: UUID
    key_id: UUID
    profile: ProductStateGitBackupProfile

    @property
    def database(self) -> Path:
        return self.root / GIT_DATABASE


def _candidate(root: Path) -> _Candidate:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    material = load_session_key(root)
    git_root = root / "git-delivery"
    git_root.mkdir(mode=0o700)
    git_root.chmod(0o700)
    database_path = root / GIT_DATABASE
    protection = _Protection()
    binding = SessionPublicationBinding(
        material.store_id, material.key_id, material.key, protection
    )
    try:
        with closing(sqlite3.connect(database_path, isolation_level=None)) as database:
            assert database.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
            database.execute("PRAGMA foreign_keys=ON")
            with git_prefix_sql_window(database, checkpoint=lambda: None):
                database.execute("BEGIN IMMEDIATE")
                initialize_git_store_v2(database)
                asyncio.run(
                    initialize_git_prefix_genesis(
                        database,
                        binding.git,
                        binding.git_prefix,
                        protection,
                        cancel=CancelToken(),
                    )
                )
                database.execute("COMMIT")

            route_id, delivery_id, thread_id, turn_id, call_id = (
                uuid4(),
                uuid4(),
                uuid4(),
                uuid4(),
                uuid4(),
            )
            payload = "{}"
            with git_prefix_sql_window(database, checkpoint=lambda: None):
                database.execute("BEGIN IMMEDIATE")
                window = begin_git_prefix_write(
                    database,
                    binding.git,
                    binding.git_prefix,
                    checkpoint=lambda: None,
                )
                database.execute(
                    "INSERT INTO git_product_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(route_id),
                        str(delivery_id),
                        str(thread_id),
                        str(turn_id),
                        str(call_id),
                        "commit",
                        "1" * 64,
                        "2" * 64,
                        "prepared",
                        0,
                        payload,
                    ),
                )
                database.execute(
                    "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
                    (str(route_id), 0, "prepared", payload),
                )
                catalog = asyncio.run(
                    publish_git_prefix_changes(
                        window,
                        (
                            GitDeliveryRecordClaims(
                                record_kind="product_link",
                                record_id=route_id,
                                publication_epoch=uuid4(),
                                sequence=1,
                                previous_sha256="0" * 64,
                                delivery_id=delivery_id,
                                thread_id=thread_id,
                                turn_id=turn_id,
                                call_id=call_id,
                                route_id=route_id,
                            ),
                        ),
                        binding.git,
                        binding.git_prefix,
                        protection,
                        cancel=CancelToken(),
                    )
                )
                database.execute("COMMIT")
        database_path.chmod(0o600)
        profile = ProductStateGitBackupProfile(
            store_genesis_epoch=catalog.genesis_epoch,
            prefix_revision=catalog.revision,
            prefix_anchor_sha256=hashlib.sha256(
                encode_git_prefix_catalog(catalog)
            ).hexdigest(),
            business_snapshot_sha256="3" * 64,
        )
        return _Candidate(root, material.store_id, material.key_id, profile)
    finally:
        binding.close()
        material.close()


@pytest.fixture
def candidate(tmp_path: Path) -> _Candidate:
    return _candidate(tmp_path / "state")


def _physical_state(candidate: _Candidate) -> tuple[bytes, int, tuple[str, ...]]:
    info = candidate.database.stat()
    names = tuple(sorted(path.name for path in candidate.database.parent.iterdir()))
    return candidate.database.read_bytes(), info.st_mtime_ns, names


def _validate(
    candidate: _Candidate,
    *,
    profile: ProductStateGitBackupProfile | None = None,
    store_id: UUID | None = None,
    key_id: UUID | None = None,
    control: MaintenanceIOControl | None = None,
) -> None:
    with PrivateStateTree(candidate.root) as tree:
        validate_git_backup_profile(
            tree,
            profile or candidate.profile,
            store_id or candidate.store_id,
            key_id or candidate.key_id,
            control or MaintenanceIOControl(),
        )


def test_authenticated_profile_verification_is_byte_and_sidecar_read_only(
    candidate: _Candidate,
) -> None:
    before = _physical_state(candidate)

    _validate(candidate)

    assert _physical_state(candidate) == before
    assert before[2] == ("git-delivery.db",)


@pytest.mark.parametrize("mismatch", ["store", "key", "genesis", "revision", "catalog_sha256"])
def test_profile_must_match_original_identity_and_canonical_catalog(
    candidate: _Candidate, mismatch: str
) -> None:
    store_id, key_id, profile = candidate.store_id, candidate.key_id, candidate.profile
    if mismatch == "store":
        store_id = uuid4()
    elif mismatch == "key":
        key_id = uuid4()
    elif mismatch == "genesis":
        profile = profile.model_copy(update={"store_genesis_epoch": uuid4()})
    elif mismatch == "revision":
        profile = profile.model_copy(update={"prefix_revision": profile.prefix_revision - 1})
    else:
        profile = profile.model_copy(update={"prefix_anchor_sha256": "4" * 64})

    before = _physical_state(candidate)
    with pytest.raises(KernelError) as caught:
        _validate(candidate, profile=profile, store_id=store_id, key_id=key_id)

    assert caught.value.code == "product_backup_state_invalid"
    assert _physical_state(candidate) == before


@pytest.mark.parametrize("damage", ["schema", "record_mac", "prefix", "tail_anchor"])
def test_exact_schema_full_prefix_record_mac_and_tail_anchor_are_required(
    candidate: _Candidate, damage: str
) -> None:
    with closing(sqlite3.connect(candidate.database)) as database:
        if damage == "schema":
            database.execute("CREATE TABLE untrusted(value TEXT)")
        elif damage == "record_mac":
            database.execute("UPDATE git_record_publications SET seal=x'00'")
        elif damage == "prefix":
            database.execute("UPDATE git_record_publications SET prefix_sha256=?", ("0" * 64,))
        else:
            database.execute("UPDATE git_prefix_anchor SET seal=x'00'")
        database.commit()

    before = _physical_state(candidate)
    with pytest.raises(KernelError) as caught:
        _validate(candidate)

    assert caught.value.code in {"git_delivery_store_corrupt", "publication_history_unproven"}
    assert _physical_state(candidate) == before


def test_wal_candidate_is_rejected_before_readonly_sqlite_can_create_sidecars(
    candidate: _Candidate,
) -> None:
    with closing(sqlite3.connect(candidate.database)) as database:
        assert database.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    assert tuple(candidate.database.parent.glob("git-delivery.db-*")) == ()
    before = _physical_state(candidate)

    with pytest.raises(KernelError) as caught:
        _validate(candidate)

    assert caught.value.code == "product_backup_state_invalid"
    assert _physical_state(candidate) == before
    assert tuple(candidate.database.parent.glob("git-delivery.db-*")) == ()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_existing_sqlite_sidecar_is_rejected_without_consumption(
    candidate: _Candidate, suffix: str
) -> None:
    sidecar = candidate.database.with_name(candidate.database.name + suffix)
    sidecar.write_bytes(b"untrusted-live-state")
    before = _physical_state(candidate), sidecar.read_bytes(), sidecar.stat().st_mtime_ns

    with pytest.raises(KernelError) as caught:
        _validate(candidate)

    assert caught.value.code == "product_backup_state_invalid"
    assert (_physical_state(candidate), sidecar.read_bytes(), sidecar.stat().st_mtime_ns) == before


@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_original_control_failure_propagates_from_authenticated_prefix_read(
    candidate: _Candidate, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    from harnessix.product_config import state_backup_v2_validation as validation

    control = MaintenanceIOControl()
    failure = KernelError(f"maintenance_io_{reason}", "原受控停止")
    stopped = False
    original = validation.read_git_prefix_catalog

    def stop_then_read(*args, **kwargs):
        nonlocal stopped
        stopped = True
        return original(*args, **kwargs)

    def checkpoint() -> None:
        if stopped:
            raise failure

    monkeypatch.setattr(validation, "read_git_prefix_catalog", stop_then_read)
    monkeypatch.setattr(control, "checkpoint", checkpoint)
    before = _physical_state(candidate)

    with pytest.raises(KernelError) as caught:
        _validate(candidate, control=control)

    assert caught.value is failure
    assert _physical_state(candidate) == before
