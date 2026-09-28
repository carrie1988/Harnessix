"""真实Runtime产物的独立Key跨重启认证；失效原行不补签。"""

from __future__ import annotations

import sqlite3
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES
from harnessix.artifacts.persistence import ARTIFACT_READ_SELECT
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.contracts import SecretReference
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.agent.test_publication import protected
from tests.artifacts.helpers import results, step

KEY = bytes(range(32))


async def _published(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.txt").write_text("needle safe\n" * 300)
    path, store_id, key_id = tmp_path / "session.db", uuid4(), uuid4()
    with protected() as scope:
        binding = SessionPublicationBinding(store_id, key_id, KEY, scope)
        try:
            store = SQLiteSessionStore(path, publication=binding)
            artifacts = SQLiteArtifactStore(store, public_output_protection=scope)
            async with CodingToolRuntime(root, artifacts=artifacts) as tools:
                async with AgentRuntime(
                    store,
                    ScriptedProvider([step(), answer()]),
                    scoped_tools=tools,
                    artifacts=artifacts,
                    public_output_protection=scope,
                ) as runtime:
                    thread = await runtime.create_thread(str(tools.workspace_root))
                    turn = await runtime.run_turn(thread.thread_id, "归档搜索", request_id="proof")
                    assert turn.status.value == "completed", turn.error
                workspace_scope = tools.workspace_scope
            ref = results(turn)[0].output["artifact"]
            with sqlite3.connect(path) as db:
                seal = db.execute("SELECT publication_seal FROM agent_artifacts").fetchone()[0]
            assert isinstance(seal, bytes) and 1 <= len(seal) <= 4096
            return (
                path,
                store_id,
                key_id,
                thread.thread_id,
                workspace_scope,
                UUID(ref["artifact_id"]),
            )
        finally:
            binding.close()


async def test_authenticated_body_survives_keyed_restart_with_new_epoch(tmp_path):
    path, store_id, key_id, thread, workspace, artifact = await _published(tmp_path)
    with protected() as scope:
        binding = SessionPublicationBinding(store_id, key_id, KEY, scope)
        try:
            store = SQLiteSessionStore(path, publication=binding)
            await store.initialize()
            reopened = SQLiteArtifactStore(store, public_output_protection=scope)
            page = await reopened.read(thread, workspace, artifact, offset=149, limit=1)
            assert "needle safe" in page.text
            assert page.artifact.artifact_id == artifact
        finally:
            binding.close()


async def test_authenticated_origin_does_not_bypass_new_secret_scope(tmp_path):
    path, store_id, key_id, thread, workspace, artifact = await _published(tmp_path)
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("api", "9", "MODEL_KEY"),),
        environment={"MODEL_KEY": "needle safe"},
    )
    with SecretPublicationScope((SecretReference(name="api", version="9"),), provider) as scope:
        binding = SessionPublicationBinding(store_id, key_id, KEY, scope)
        try:
            store = SQLiteSessionStore(path, publication=binding)
            await store.initialize()
            reopened = SQLiteArtifactStore(store, public_output_protection=scope)
            with pytest.raises(KernelError) as denied:
                await reopened.read(thread, workspace, artifact)
            assert denied.value.code == "public_output_secret_leak"
        finally:
            binding.close()


async def test_keyed_issue_rejects_different_protection_scope(tmp_path):
    with protected() as original, protected() as substitute:
        binding = SessionPublicationBinding(uuid4(), uuid4(), KEY, original)
        try:
            store = SQLiteSessionStore(tmp_path / "session.db", publication=binding)
            artifacts = SQLiteArtifactStore(store, public_output_protection=substitute)
            with pytest.raises(KernelError) as denied:
                await artifacts._publication.check_body(b'{"text":"safe"}\n')
            assert denied.value.code == "publication_scope_changed"
        finally:
            binding.close()


async def test_migration30_does_not_resign_existing_keyed_artifact(tmp_path):
    path, store_id, key_id, thread, workspace, artifact = await _published(tmp_path)
    with sqlite3.connect(path) as db:
        original_body = db.execute("SELECT body FROM agent_artifacts").fetchone()[0]
        original_events = db.execute(
            "SELECT event_json FROM agent_events ORDER BY sequence"
        ).fetchall()
        db.execute("ALTER TABLE agent_artifacts DROP COLUMN publication_seal")
        db.execute("DELETE FROM agent_migrations WHERE version=30")
    with protected() as scope:
        binding = SessionPublicationBinding(store_id, key_id, KEY, scope)
        try:
            store = SQLiteSessionStore(path, publication=binding)
            await store.initialize()
            artifacts = SQLiteArtifactStore(store, public_output_protection=scope)
            with pytest.raises(KernelError) as denied:
                await artifacts.read(thread, workspace, artifact)
            assert denied.value.code == "artifact_publication_unproven"
        finally:
            binding.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT body,publication_seal FROM agent_artifacts").fetchone() == (
            original_body,
            None,
        )
        assert db.execute("SELECT event_json FROM agent_events ORDER BY sequence").fetchall() == (
            original_events
        )


async def test_corrupt_oversized_body_is_bounded_before_python_read(tmp_path):
    path, store_id, key_id, thread, workspace, artifact = await _published(tmp_path)
    with protected() as scope:
        binding = SessionPublicationBinding(store_id, key_id, KEY, scope)
        try:
            store = SQLiteSessionStore(path, publication=binding)
            await store.initialize()
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA ignore_check_constraints=ON")
                db.execute(
                    "UPDATE agent_artifacts SET body=zeroblob(?)", (MAX_ARTIFACT_BYTES + 100,)
                )
            artifacts = SQLiteArtifactStore(store, public_output_protection=scope)
            async with store._connection() as db:
                row = await (await db.execute(ARTIFACT_READ_SELECT)).fetchone()
                assert row is not None and len(row["body"]) == MAX_ARTIFACT_BYTES + 1
            with pytest.raises(KernelError) as denied:
                await artifacts.read(thread, workspace, artifact)
            assert denied.value.code == "artifact_publication_unproven"
        finally:
            binding.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("publication_seal", None),
        ("body", "flip"),
        ("manifest_json", "{}"),
        ("created_at", "2000-01-01T00:00:00+00:00"),
        ("publication_epoch", str(uuid4())),
        ("workspace_scope", "2" * 64),
    ],
)
async def test_modified_or_missing_original_proof_fails_without_resigning(tmp_path, column, value):
    path, store_id, key_id, thread, workspace, artifact = await _published(tmp_path)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT publication_seal FROM agent_artifacts").fetchone()[0]
        if column == "body":
            body = db.execute("SELECT body FROM agent_artifacts").fetchone()[0]
            value = bytes([body[0] ^ 1]) + body[1:]
        db.execute(f"UPDATE agent_artifacts SET {column}=?", (value,))  # noqa: S608
        original = db.execute("SELECT * FROM agent_artifacts").fetchone()
    with protected() as scope:
        binding = SessionPublicationBinding(store_id, key_id, KEY, scope)
        try:
            store = SQLiteSessionStore(path, publication=binding)
            await store.initialize()
            reopened = SQLiteArtifactStore(store, public_output_protection=scope)
            async with store._connection() as db:
                row = await (await db.execute("SELECT * FROM agent_artifacts")).fetchone()
                assert row is not None
                with pytest.raises(KernelError) as denied:
                    reopened._publication.require_proof(row)
                assert denied.value.code == "artifact_publication_unproven"
            if column != "workspace_scope":
                with pytest.raises(KernelError):
                    await reopened.read(thread, workspace, artifact)
        finally:
            binding.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM agent_artifacts").fetchone() == original
        if column != "publication_seal":
            assert (
                db.execute("SELECT publication_seal FROM agent_artifacts").fetchone()[0] == before
            )
