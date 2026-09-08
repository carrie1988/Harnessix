from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.sandbox.contracts import NetworkPolicy
from harnessix.sandbox.network import resolve_network_policy
from harnessix.sandbox.planner import build_container_sandbox_profile
from harnessix.sandbox.store import SQLiteSandboxProfileStore


def _profile():
    network = resolve_network_policy(
        NetworkPolicy(mode="none"), now=datetime(2026, 9, 8, tzinfo=UTC)
    )
    return build_container_sandbox_profile(
        image="sha256:" + "a" * 64,
        workspace_mode="read_only",
        network=network,
    )


def test_sandbox_profile_is_durable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "private/sandbox.db"
    profile = _profile()
    with SQLiteSandboxProfileStore(path) as store:
        store.save(profile)
        store.save(profile)
    with SQLiteSandboxProfileStore(path) as reopened:
        assert reopened.load(profile.digest) == profile


def test_sandbox_store_fails_closed_on_unknown_version_and_corruption(tmp_path: Path) -> None:
    path = tmp_path / "private/sandbox.db"
    path.parent.mkdir()
    database = sqlite3.connect(path)
    database.execute(
        "CREATE TABLE sandbox_store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    database.execute("INSERT INTO sandbox_store_metadata VALUES ('schema_version', '2')")
    database.commit()
    database.close()
    with pytest.raises(KernelError) as version:
        SQLiteSandboxProfileStore(path)
    assert version.value.code == "sandbox_store_version"

    path.unlink()
    profile = _profile()
    with SQLiteSandboxProfileStore(path) as store:
        store.save(profile)
        store._db.execute(  # noqa: SLF001 - 故障注入验证损坏记录失败关闭
            "UPDATE sandbox_profiles SET payload = '{}' WHERE digest = ?", (profile.digest,)
        )
        with pytest.raises(KernelError) as corrupt:
            store.load(profile.digest)
    assert corrupt.value.code == "sandbox_store_corrupt"
