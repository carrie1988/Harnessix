"""原事务 CAS 写权限与耐久入口；只读校验不得先落正文再被 SQLite 拒绝。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import store as store_module
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from tests.delivery.test_store import _prepared


def test_readonly_save_rejects_before_persisting_any_new_blob(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepared = _prepared(workspace)
    root = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(root):
        pass
    with SQLiteWorkspaceTransactionStore(root, read_only=True) as reader:
        with pytest.raises(KernelError) as failure:
            reader.save(prepared)
        assert failure.value.code == "delivery_store_read_only"
    assert tuple((root / "blobs").iterdir()) == ()


def test_readonly_private_blob_entry_rejects_before_any_write(tmp_path: Path) -> None:
    root = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(root):
        pass
    body = b"new-blob-only"
    with SQLiteWorkspaceTransactionStore(root, read_only=True) as reader:
        with pytest.raises(KernelError) as failure:
            reader._put_blob(hashlib.sha256(body).hexdigest(), body)
        assert failure.value.code == "delivery_store_read_only"
    assert tuple((root / "blobs").iterdir()) == ()


@pytest.mark.parametrize("operation", ["put", "private-put", "save", "transition"])
@pytest.mark.parametrize("authority", ["read-only", "closed"])
def test_write_authority_precedes_input_validation_and_file_creation(
    tmp_path, operation, authority
) -> None:
    root = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(root):
        pass
    store = SQLiteWorkspaceTransactionStore(root, read_only=authority == "read-only")
    try:
        if authority == "closed":
            store.close()
        actions = {
            "put": lambda: store.put_blob("not-a-digest", None),
            "private-put": lambda: store._put_blob("not-a-digest", None),
            "save": lambda: store.save(None),
            "transition": lambda: store.transition(None, None),
        }
        with pytest.raises(KernelError) as failure:
            actions[operation]()
        assert failure.value.code == (
            "delivery_store_read_only" if authority == "read-only" else "delivery_store_closed"
        )
        assert tuple((root / "blobs").iterdir()) == ()
    finally:
        store.close()


def test_existing_blob_durability_is_confirmed_without_replacement(tmp_path, monkeypatch) -> None:
    root = tmp_path / "state"
    body = b"existing-cas-body"
    digest = hashlib.sha256(body).hexdigest()
    with SQLiteWorkspaceTransactionStore(root) as store:
        store._put_blob(digest, body)
        target = root / "blobs" / digest
        before = target.stat()
        events = []
        original_sync = os.fsync
        original_directory = store_module._fsync_directory

        def observe_sync(descriptor):
            events.append("file")
            original_sync(descriptor)

        def observe_directory(path):
            events.append("directory")
            original_directory(path)

        monkeypatch.setattr(store_module.os, "fsync", observe_sync)
        monkeypatch.setattr(store_module, "_fsync_directory", observe_directory)
        store.put_blob(digest, body)
        after = target.stat()
        assert events[:2] == ["file", "directory"]
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        assert store.blob(digest) == body and len(tuple((root / "blobs").iterdir())) == 1


@pytest.mark.parametrize("fault", ["file-sync", "directory-sync", "final-readback"])
def test_existing_blob_confirmation_failure_is_not_reported_success(
    tmp_path, monkeypatch, fault
) -> None:
    root = tmp_path / "state"
    body = b"durability-fault-body"
    digest = hashlib.sha256(body).hexdigest()
    with SQLiteWorkspaceTransactionStore(root) as store:
        store._put_blob(digest, body)
        target = root / "blobs" / digest
        before = target.stat()

        def fail(*_):
            raise OSError("injected persistence confirmation failure")

        if fault == "file-sync":
            monkeypatch.setattr(store_module.os, "fsync", fail)
        elif fault == "directory-sync":
            monkeypatch.setattr(store_module, "_fsync_directory", fail)
        else:
            original_blob = store.blob
            calls = 0

            def changed(digest):
                nonlocal calls
                calls += 1
                return original_blob(digest) if calls == 1 else b"changed-body"

            monkeypatch.setattr(store, "blob", changed)
        with pytest.raises(KernelError) as failure:
            store.put_blob(digest, body)
        assert failure.value.code == "delivery_storage_unavailable"
        assert "injected persistence" not in str(failure.value)
        after = target.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        assert target.read_bytes() == body


@pytest.mark.parametrize("body", [b"", b"x" * (8 * 1024 * 1024)], ids=["empty", "8MiB"])
def test_public_blob_accepts_exact_existing_capacity_and_reopens(tmp_path, body) -> None:
    root = tmp_path / "state"
    digest = hashlib.sha256(body).hexdigest()
    with SQLiteWorkspaceTransactionStore(root) as writer:
        writer.put_blob(digest, body)
    with SQLiteWorkspaceTransactionStore(root, read_only=True) as reader:
        assert reader.blob(digest) == body
        assert reader._db.execute("SELECT count(*) FROM workspace_transactions").fetchone() == (0,)


@pytest.mark.parametrize("entry", ["public-store", "typed-cas"])
def test_exclusive_temporary_collision_preserves_unowned_file(tmp_path, monkeypatch, entry) -> None:
    body = b"new-cas-material"
    digest = hashlib.sha256(body).hexdigest()
    nonce = UUID(int=1)
    root = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(root) as store:
        temporary = root / "blobs" / f".{digest}.{nonce.hex}.tmp"
        temporary.write_bytes(b"foreign-temporary")
        before = temporary.stat()
        monkeypatch.setattr(store_module, "uuid4", lambda: nonce)
        with pytest.raises(KernelError) as failure:
            if entry == "public-store":
                store.put_blob(digest, body)
            else:
                oid = hashlib.sha1(f"blob {len(body)}\0".encode("ascii") + body).hexdigest()
                GitMaterialCAS(store).persist(GitObjectMaterial("blob", oid, "sha1", body))
        assert failure.value.code == (
            "delivery_storage_unavailable"
            if entry == "public-store"
            else "git_material_cas_write_failed"
        )
        assert temporary.stat() == before
        assert temporary.read_bytes() == b"foreign-temporary"
        assert sorted((root / "blobs").iterdir()) == [temporary]
        assert not (root / "blobs" / digest).exists()
