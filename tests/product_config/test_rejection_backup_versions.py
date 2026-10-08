"""备份冻结v30/v31迁移集合、原认证字节恢复及正式升级的离线矩阵。"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import closing
from importlib.resources import files
from uuid import uuid4

import pytest

from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config import server as product
from harnessix.product_config.runtime import build_provider_bundle
from harnessix.product_config.state_backup import backup_product_state, verify_product_backup
from harnessix.product_config.state_backup_files import PrivateStateTree
from harnessix.product_config.state_backup_validation import (
    _SESSION_MIGRATION_CHECKSUMS,
    _session_schema,
    original_key,
)
from harnessix.product_config.state_restore import restore_product_state
from tests.product_config.conftest import write_config
from tests.session.test_authenticated_history import binding, business_rows, read
from tests.session.test_rejection_publication_versions import (
    calling_model,
    original_v20_store,
    raises_code,
    rejection_batch,
)


def resource_checksums():
    return {
        int(resource.name.split("_", 1)[0]): hashlib.sha256(resource.read_bytes()).hexdigest()
        for resource in files("harnessix.session.migrations").iterdir()
        if resource.name.endswith(".sql")
    }


def schema_fixture(database, version):
    database.execute("PRAGMA application_id=1213748043")
    database.execute("CREATE TABLE agent_migrations(version INTEGER PRIMARY KEY,checksum TEXT)")
    database.execute("CREATE TABLE agent_threads(projection_version INTEGER)")
    database.execute("CREATE TABLE agent_events(event_json TEXT)")
    checksums = resource_checksums()
    database.executemany(
        "INSERT INTO agent_migrations VALUES (?,?)",
        [(index, checksums.get(index, "0" * 64)) for index in range(1, version + 1)],
    )


def test_resource_checksums_equal_frozen_complete_v31_contract():
    assert resource_checksums() == dict(enumerate(_SESSION_MIGRATION_CHECKSUMS, 1))
    assert len(_SESSION_MIGRATION_CHECKSUMS) == 31
    sql = (
        files("harnessix.session.migrations")
        .joinpath("0031_unknown_tool_rejection.sql")
        .read_text()
    )
    assert "\n".join(line for line in sql.splitlines() if not line.startswith("--")).strip() == (
        "SELECT 1;"
    )


@pytest.mark.parametrize("version", range(33))
def test_backup_only_accepts_complete_v30_or_v31_not_arbitrary_prefix(version):
    with closing(sqlite3.connect(":memory:")) as database:
        schema_fixture(database, version)
        if version in (30, 31):
            _session_schema(database)
        else:
            with raises_code("product_backup_state_invalid"):
                _session_schema(database)


@pytest.mark.parametrize(
    ("version", "changed"),
    [(version, changed) for version in (30, 31) for changed in range(1, version + 1)],
)
def test_every_applied_checksum_is_frozen(version, changed):
    with closing(sqlite3.connect(":memory:")) as database:
        schema_fixture(database, version)
        database.execute(
            "UPDATE agent_migrations SET checksum=? WHERE version=?", ("f" * 64, changed)
        )
        with raises_code("product_backup_state_invalid"):
            _session_schema(database)


@pytest.mark.parametrize("version", [30, 31])
@pytest.mark.parametrize("missing", [1, 15, 30])
@pytest.mark.parametrize("replace", [False, True])
def test_gap_or_unknown_version_rejects_even_with_supported_row_count(version, missing, replace):
    with closing(sqlite3.connect(":memory:")) as database:
        schema_fixture(database, version)
        database.execute("DELETE FROM agent_migrations WHERE version=?", (missing,))
        if replace:
            database.execute("INSERT INTO agent_migrations VALUES (32,?)", ("0" * 64,))
        with raises_code("product_backup_state_invalid"):
            _session_schema(database)


@pytest.mark.parametrize("version", [30, 31])
@pytest.mark.parametrize("application_id", [0, 42])
def test_backup_database_identity_is_not_relaxed(version, application_id):
    with closing(sqlite3.connect(":memory:")) as database:
        schema_fixture(database, version)
        database.execute(f"PRAGMA application_id={application_id}")
        with raises_code("product_backup_state_invalid"):
            _session_schema(database)


@pytest.mark.parametrize("version", [30, 31])
def test_duplicate_migration_rows_cannot_be_collapsed_into_supported_collection(version):
    with closing(sqlite3.connect(":memory:")) as database:
        schema_fixture(database, version)
        database.execute("ALTER TABLE agent_migrations RENAME TO original_migrations")
        database.execute("CREATE TABLE agent_migrations(version INTEGER,checksum TEXT)")
        database.execute("INSERT INTO agent_migrations SELECT * FROM original_migrations")
        database.execute(
            "INSERT INTO agent_migrations SELECT * FROM original_migrations WHERE version=1"
        )
        with raises_code("product_backup_state_invalid"):
            _session_schema(database)


@pytest.mark.parametrize("fact", ["event", "projection"])
def test_v30_backup_cannot_hide_v21_facts_without_minimum_reader_marker(fact):
    with closing(sqlite3.connect(":memory:")) as database:
        schema_fixture(database, 30)
        if fact == "event":
            database.execute(
                "INSERT INTO agent_events VALUES (?)", (json.dumps({"schema_version": 21}),)
            )
        else:
            database.execute("INSERT INTO agent_threads VALUES (21)")
        with raises_code("product_backup_state_invalid"):
            _session_schema(database)


@pytest.fixture
async def empty_product_state(tmp_path, config, monkeypatch):
    """正式默认装配创建完整Store，固定Provider从不接收模型请求。"""
    root, workspace = tmp_path / "state", tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PRIMARY_API_KEY", "rejection-backup-primary-fixture")
    monkeypatch.setenv("BACKUP_API_KEY", "rejection-backup-secondary-fixture")

    async def build(snapshot, selection, secrets, *, audit):
        return await build_provider_bundle(
            snapshot, selection, secrets, audit=audit, factory=lambda *_: ScriptedProvider([])
        )

    async def idle(*_):
        pass

    monkeypatch.setattr(product, "build_provider_bundle", build)
    monkeypatch.setattr(product, "run_stdio", idle)
    await product.run_product_stdio(
        config_path=write_config(tmp_path / "config.json", config),
        profile_id=None,
        workspace=workspace,
        state_directory=root,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    return root


@pytest.mark.parametrize("version", [30, 31])
async def test_full_backup_restore_keeps_original_bytes_then_formal_initialize_upgrades(
    empty_product_state, tmp_path, version
):
    root = empty_product_state
    with PrivateStateTree(root) as tree, closing(original_key(tree)) as material:
        with binding(material.store_id, material.key_id, key=bytes(material.key)) as proof:
            store, tid, original = await original_v20_store(
                root / "sessions.db", proof, workspace=root.parent / "workspace"
            )
            if version == 31:
                await store.initialize()
                turn_id, sequence = await calling_model(store, tid, 1)
                original = await store.append(
                    tid, rejection_batch(turn_id), expected_sequence=sequence
                )
            before = business_rows(store.path)
            destination = tmp_path / "backup"
            manifest = await backup_product_state(root, destination)
            assert await verify_product_backup(root, destination) == manifest
            assert business_rows(store.path) == before
            candidate = destination / "state"
            assert business_rows(candidate / "sessions.db") == before
            result = await restore_product_state(
                root, destination, restore_id=uuid4(), confirm_backup_id=manifest.backup_id
            )
            assert result is not None
            assert (root / "sessions.db").read_bytes() == (candidate / "sessions.db").read_bytes()
            assert (root / "session-auth/key.v1").read_bytes() == (
                candidate / "session-auth/key.v1"
            ).read_bytes()
            assert business_rows(store.path) == before
            assert (await read(store, tid)).thread == original
            await store.initialize()
            after = business_rows(store.path)
            assert after["agent_migrations"][-1][0] == 31
            for name in before.keys() - {"agent_migrations"}:
                assert after[name] == before[name]
