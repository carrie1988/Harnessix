"""集中写入边界验证原JSONL与同事务证明；不将其冒充各业务生产者集成验收。"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.publication import PUBLIC_PROTECTION_POLICY
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.persistence import insert_artifact
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.domain.models import utc_now
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.test_publication import CANARY, protected


@pytest.mark.parametrize("purpose", ["tool_result", "action_review", "action_output", "batch_plan"])
@pytest.mark.parametrize("case", ["safe", "leak"])
async def test_guard_and_proof_share_insert_transaction(tmp_path, purpose, case):
    store = SQLiteSessionStore(tmp_path / "s.db")
    with protected() as scope:
        guard = ArtifactPublicationGuard(scope)
        async with AgentRuntime(store, ScriptedProvider([])) as agent:
            thread = await agent.create_thread(str(tmp_path))
            body = ('{"text":"' + (CANARY if case == "leak" else "benign") + '"}\n').encode()
            ref = ArtifactRef(
                artifact_id=uuid4(),
                sha256=hashlib.sha256(body).hexdigest(),
                size_bytes=len(body),
                records=1,
                complete=True,
                expires_at=utc_now() + timedelta(hours=1),
            )

            async def publish():
                async with store._connection() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    await insert_artifact(
                        db,
                        ref,
                        thread_id=thread.thread_id,
                        turn_id=uuid4(),
                        call_id=uuid4(),
                        workspace_scope="1" * 64,
                        body=body,
                        purpose=purpose,
                        created_at=utc_now(),
                        publication=guard,
                    )
                    await db.commit()

            if case == "leak":
                with pytest.raises(KernelError) as caught:
                    await publish()
                assert caught.value.code == "public_output_secret_leak"
            else:
                await publish()
        with sqlite3.connect(store.path) as db:
            rows = db.execute(
                "SELECT body,publication_epoch,publication_policy FROM agent_artifacts"
            ).fetchall()
            assert rows == (
                [] if case == "leak" else [(body, guard.epoch, PUBLIC_PROTECTION_POLICY)]
            )
            assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "epoch,policy",
    [
        (str(uuid4()), None),
        (None, PUBLIC_PROTECTION_POLICY),
        (str(uuid4()), "unrecognized"),
        ("short", PUBLIC_PROTECTION_POLICY),
    ],
)
async def test_migration_rejects_partial_or_unknown_publication_proof(tmp_path, epoch, policy):
    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.initialize()
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                "INSERT INTO agent_artifacts "
                "(artifact_id,thread_id,turn_id,call_id,workspace_scope,manifest_json,size_bytes,"
                "expires_at,state,body,purpose,created_at,publication_epoch,publication_policy) "
                "VALUES (?,?,?,?,?,?,?,?,'published',?,?,?,?,?)",
                tuple(
                    [str(uuid4())] * 4
                    + ["1" * 64, "{}", 1, "date", b"\n", "tool_result", "date", epoch, policy]
                ),
            )
