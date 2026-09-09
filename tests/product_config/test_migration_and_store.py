from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.codec import decode_product_config_bytes, load_product_config
from harnessix.product_config.contracts import (
    ConfigMigrationReceipt,
    ProductConfigSnapshot,
    ProductConfigV1,
    ProductConfigV2,
)
from harnessix.product_config.migration import migrate_product_config_file, migrate_v1
from harnessix.product_config.store import SQLiteProductConfigStore
from tests.product_config.conftest import write_config


def legacy_body(config: ProductConfigV2) -> bytes:
    providers = []
    sources = {item.secret.name: item for item in config.secret_sources}
    for provider in config.providers:
        providers.append(
            {
                "provider_id": provider.provider_id,
                "kind": provider.kind,
                "base_url": provider.base_url,
                "api_key_env": sources[provider.credential.name].environment_variable,
                "output_token_parameter": provider.output_token_parameter,
            }
        )
    return (
        json.dumps(
            {
                "spec_version": "harnessix.product-config/v1",
                "active_profile": config.active_profile,
                "providers": providers,
                "profiles": [item.model_dump(mode="json") for item in config.profiles],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()


def test_migrates_with_backup_cas_and_idempotent_reopen(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = tmp_path / "config.json"
    source = legacy_body(config)
    path.write_bytes(source)
    path.chmod(0o600)
    source_sha256 = hashlib.sha256(source).hexdigest()

    receipt = migrate_product_config_file(path, expected_source_sha256=source_sha256)

    assert receipt.changed and receipt.source_sha256 == receipt.backup_sha256 == source_sha256
    assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt.target_sha256
    backup = tmp_path / f"config.json.v1.{hashlib.sha256(source).hexdigest()}.bak"
    assert backup.read_bytes() == source
    loaded = load_product_config(path)
    assert isinstance(loaded, ProductConfigSnapshot)
    legacy = decode_product_config_bytes(source, allow_legacy=True)
    assert isinstance(legacy, ProductConfigV1)
    assert loaded.config == migrate_v1(legacy)
    second = migrate_product_config_file(path, expected_source_sha256=receipt.target_sha256)
    assert not second.changed and second.from_version == "v2"

    invalid = second.model_copy(update={"target_sha256": "0" * 64})
    with pytest.raises(ValueError):
        ConfigMigrationReceipt.model_validate_json(invalid.model_dump_json())


def test_migration_deduplicates_shared_environment_secret(
    config: ProductConfigV2,
) -> None:
    legacy = decode_product_config_bytes(legacy_body(config), allow_legacy=True)
    assert isinstance(legacy, ProductConfigV1)
    shared_environment = legacy.providers[0].api_key_env
    shared = legacy.model_copy(
        update={
            "providers": (
                legacy.providers[0],
                legacy.providers[1].model_copy(update={"api_key_env": shared_environment}),
            )
        }
    )

    migrated = migrate_v1(shared)

    assert len(migrated.secret_sources) == 1
    assert migrated.secret_sources[0].environment_variable == shared_environment
    assert migrated.providers[0].credential == migrated.providers[1].credential


def test_migration_conflict_and_crash_before_replace_are_recoverable(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = tmp_path / "config.json"
    source = legacy_body(config)
    path.write_bytes(source)
    path.chmod(0o600)
    digest = hashlib.sha256(source).hexdigest()
    with pytest.raises(KernelError) as error:
        migrate_product_config_file(path, expected_source_sha256="0" * 64)
    assert error.value.code == "product_config_conflict"

    def fail(point: str) -> None:
        if point == "migration.after_backup_fsync":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        migrate_product_config_file(path, expected_source_sha256=digest, fault=fail)
    assert path.read_bytes() == source
    receipt = migrate_product_config_file(path, expected_source_sha256=digest)
    assert receipt.changed


def test_crash_after_replace_recovers_as_idempotent_v2(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = tmp_path / "config.json"
    source = legacy_body(config)
    path.write_bytes(source)
    path.chmod(0o600)
    source_sha256 = hashlib.sha256(source).hexdigest()

    def fail(point: str) -> None:
        if point == "migration.after_replace_fsync":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        migrate_product_config_file(
            path,
            expected_source_sha256=source_sha256,
            fault=fail,
        )
    target_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    reopened = migrate_product_config_file(
        path,
        expected_source_sha256=target_sha256,
    )
    assert not reopened.changed and reopened.from_version == "v2"
    assert reopened.source_sha256 == reopened.target_sha256 == target_sha256


def test_migration_rejects_linked_or_insecure_lock(tmp_path: Path, config: ProductConfigV2) -> None:
    if os.name != "posix":
        pytest.skip("POSIX硬链接与权限语义")
    path = tmp_path / "config.json"
    source = legacy_body(config)
    path.write_bytes(source)
    path.chmod(0o600)
    digest = hashlib.sha256(source).hexdigest()
    lock = tmp_path / ".config.json.migration.lock"
    os.link(path, lock)
    with pytest.raises(KernelError) as error:
        migrate_product_config_file(path, expected_source_sha256=digest)
    assert error.value.code == "product_config_lock"
    lock.unlink()
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o644)
    with pytest.raises(KernelError) as error:
        migrate_product_config_file(path, expected_source_sha256=digest)
    assert error.value.code == "product_config_lock"


def test_store_activation_cas_hash_chains_and_corruption(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = write_config(tmp_path / "config.json", config)
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    with SQLiteProductConfigStore(tmp_path / "state" / "config.db") as store:
        store.save_snapshot(snapshot)
        event = store.activate(snapshot, "primary", expected_active_sha256=None)
        assert event is not None and event.operation == "activated"
        assert store.activate(snapshot, "primary", expected_active_sha256=None) is None
        assert store.active() == (snapshot.config_sha256, "primary")
        assert [item.operation for item in store.config_events()] == ["loaded", "activated"]
        with pytest.raises(KernelError) as error:
            store.activate(snapshot, "backup", expected_active_sha256="0" * 64)
        assert error.value.code == "product_config_conflict"
        changed = store.activate(
            snapshot,
            "backup",
            expected_active_sha256=snapshot.config_sha256,
            expected_active_profile="primary",
        )
        assert changed is not None
        assert changed.previous_active_sha256 == snapshot.config_sha256
        assert changed.previous_active_profile == "primary"
        with pytest.raises(KernelError) as error:
            store.activate(
                snapshot,
                "primary",
                expected_active_sha256=snapshot.config_sha256,
                expected_active_profile="primary",
            )
        assert error.value.code == "product_config_conflict"
        store._db.execute(
            "UPDATE product_config_events SET digest = ? WHERE sequence = 1", ("0" * 64,)
        )
        with pytest.raises(KernelError) as error:
            store.config_events()
        assert error.value.code == "product_config_store_corrupt"
        before = store._db.execute("SELECT COUNT(*) FROM product_config_events").fetchone()
        with pytest.raises(KernelError) as error:
            store.activate(
                snapshot,
                "primary",
                expected_active_sha256=snapshot.config_sha256,
                expected_active_profile="backup",
            )
        assert error.value.code == "product_config_store_corrupt"
        after = store._db.execute("SELECT COUNT(*) FROM product_config_events").fetchone()
        assert before == after
        with pytest.raises(KernelError) as error:
            store.record_fallback(
                config_sha256=snapshot.config_sha256,
                thread_id=uuid4(),
                turn_id=uuid4(),
                step=1,
                from_profile="primary",
                from_provider="primary",
                to_profile="backup",
                to_provider="backup",
                failure_code="transport",
            )
        assert error.value.code == "product_config_store_corrupt"


def test_active_profile_and_fallback_graph_corruption_fail_closed(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    snapshot = load_product_config(write_config(tmp_path / "config.json", config))
    assert isinstance(snapshot, ProductConfigSnapshot)
    with SQLiteProductConfigStore(tmp_path / "active.db") as store:
        store.save_snapshot(snapshot)
        store.activate(snapshot, "primary", expected_active_sha256=None)
        store._db.execute(
            "UPDATE product_config_active SET profile_id = 'missing' WHERE singleton = 1"
        )
        with pytest.raises(KernelError) as error:
            store.active()
        assert error.value.code == "product_config_store_corrupt"

    with SQLiteProductConfigStore(tmp_path / "fallback-graph.db") as store:
        store.save_snapshot(snapshot)
        with pytest.raises(KernelError) as error:
            store.record_fallback(
                config_sha256=snapshot.config_sha256,
                thread_id=uuid4(),
                turn_id=uuid4(),
                step=1,
                from_profile="backup",
                from_provider="backup",
                to_profile="primary",
                to_provider="primary",
                failure_code="transport",
            )
        assert error.value.code == "product_config_fallback_invalid"
        assert store.fallback_events() == ()


def test_store_rejects_shared_parent_and_linked_database(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX所有者、权限与硬链接语义")
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(KernelError) as error:
        SQLiteProductConfigStore(shared / "config.db")
    assert error.value.code == "product_config_store_permissions"
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755

    private = tmp_path / "private"
    with SQLiteProductConfigStore(private / "config.db"):
        pass
    linked = private / "linked.db"
    os.link(private / "config.db", linked)
    with pytest.raises(KernelError) as error:
        SQLiteProductConfigStore(private / "config.db")
    assert error.value.code == "product_config_store_permissions"


def test_fallback_audit_chain_detects_payload_and_head_corruption(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    snapshot = load_product_config(write_config(tmp_path / "config.json", config))
    assert isinstance(snapshot, ProductConfigSnapshot)
    with SQLiteProductConfigStore(tmp_path / "fallback.db") as store:
        store.save_snapshot(snapshot)
        decision = store.record_fallback(
            config_sha256=snapshot.config_sha256,
            thread_id=uuid4(),
            turn_id=uuid4(),
            step=1,
            from_profile="primary",
            from_provider="primary",
            to_profile="backup",
            to_provider="backup",
            failure_code="rate_limit",
        )
        assert store.fallback_events() == (decision,)
        store._db.execute(
            "UPDATE provider_fallback_head SET digest = ? WHERE singleton = 1",
            ("0" * 64,),
        )
        with pytest.raises(KernelError) as error:
            store.fallback_events()
        assert error.value.code == "product_config_store_corrupt"
        before = store._db.execute("SELECT COUNT(*) FROM provider_fallback_events").fetchone()
        with pytest.raises(KernelError) as error:
            store.record_fallback(
                config_sha256=snapshot.config_sha256,
                thread_id=uuid4(),
                turn_id=uuid4(),
                step=2,
                from_profile="primary",
                from_provider="primary",
                to_profile="backup",
                to_provider="backup",
                failure_code="transport",
            )
        assert error.value.code == "product_config_store_corrupt"
        after = store._db.execute("SELECT COUNT(*) FROM provider_fallback_events").fetchone()
        assert before == after
