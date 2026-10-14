"""搜索快照的宿主版本、旧合同及Scoped Artifact接线。"""

import hashlib
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import prepare_model_history
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import GrepSnapshotMatch, GrepSnapshotOutput
from tests.agent.helpers import answer
from tests.artifacts.helpers import results, step
from tests.tools.test_files import call, execute


async def test_snapshot_mode_binds_new_contract_and_preserves_default(tmp_path):
    body = b"before\r\nneedle = 1\r\nafter\r\n"
    (tmp_path / "source.py").write_bytes(body)
    async with CodingToolRuntime(tmp_path) as legacy:
        old_definition = next(d for d in legacy.definitions() if d.name == "grep")
        old_call = call(legacy, "grep", query="needle")
        old = await legacy.execute(old_call, CancelToken())
    async with CodingToolRuntime(tmp_path, enable_grep_snapshots=True) as candidate:
        definition = next(d for d in candidate.definitions() if d.name == "grep")
        assert definition.version.startswith("3.") and old_definition.version.startswith("2.")
        assert definition.input_schema["properties"]["context_lines"]["default"] == 2
        assert definition.input_schema["properties"]["max_results"]["default"] == 10
        with pytest.raises(KernelError, match="已变化"):
            await candidate.execute(old_call, CancelToken())
        new = await execute(candidate, "grep", query="needle")
    assert old.outcome == new.outcome == "succeeded"
    assert "spec_version" not in old.output
    assert "content_sha256" not in old.output["matches"][0]
    hit = new.output["matches"][0]
    assert hit["content_sha256"] == hashlib.sha256(body).hexdigest()
    assert hit["file_bytes"] == len(body)
    assert hit["revision"] != hit["content_sha256"]
    assert len(hit["context"]["lines"]) == 3


@pytest.mark.parametrize("invalid", [None, 1, "true", []])
def test_invalid_snapshot_flag_fails_before_opening_workspace(tmp_path, invalid):
    with pytest.raises(KernelError) as error:
        CodingToolRuntime(tmp_path / "missing", enable_grep_snapshots=invalid)
    assert error.value.code == "tool_snapshot_policy_invalid"


@pytest.mark.parametrize("enabled", [False, True])
async def test_snapshot_mode_does_not_broaden_path_authority(tmp_path, enabled):
    (tmp_path / "secret.py").write_text("needle\n")
    (tmp_path / "visible.py").write_text("needle\n")
    (tmp_path / "linked.py").symlink_to("secret.py")
    async with CodingToolRuntime(
        tmp_path, denied_paths=("secret.py",), enable_grep_snapshots=enabled
    ) as tools:
        result = await execute(tools, "grep", query="needle")
    assert result.outcome == "succeeded"
    assert [hit["path"] for hit in result.output["matches"]] == ["visible.py"]


async def test_snapshot_archive_ownership_omission_and_replay_preserve_full_observation(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    body = ("needle 中文\r\n" * 40).encode()
    (root / "source.py").write_bytes(body)
    store = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(store)
    policy = ToolResultViewPolicy(max_inline_utf8_bytes=2048)
    async with CodingToolRuntime(root, artifacts=artifacts, enable_grep_snapshots=True) as tools:
        provider = ScriptedProvider([step(query="needle", max_results=10), answer()])
        async with AgentRuntime(
            store, provider, scoped_tools=tools, artifacts=artifacts, tool_result_view_policy=policy
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "归档搜索快照", request_id="snapshot")
        scope = tools.workspace_scope
    output = results(turn)[0].output
    preview = GrepSnapshotOutput.model_validate_json(json.dumps(output["preview"]))
    assert preview.truncated and len(preview.matches) == 10
    ref = output["artifact"]
    assert ref["complete"] and ref["records"] == 40
    page = await artifacts.read(thread.thread_id, scope, UUID(ref["artifact_id"]))
    assert hashlib.sha256(page.text.encode()).hexdigest() == ref["sha256"]
    hits = [GrepSnapshotMatch.model_validate_json(line) for line in page.text.splitlines()]
    assert [hit.line for hit in hits] == list(range(1, 41))
    assert all(hit.content_sha256 == hashlib.sha256(body).hexdigest() for hit in hits)
    assert all(hit.file_bytes == len(body) for hit in hits)
    with pytest.raises(KernelError):
        await artifacts.read(uuid4(), scope, UUID(ref["artifact_id"]))
    with pytest.raises(KernelError):
        await artifacts.read(thread.thread_id, "f" * 64, UUID(ref["artifact_id"]))

    reopened = await store.get_thread(thread.thread_id)
    before = reopened.model_dump_json()
    assert replay(await store.events(thread.thread_id)) == reopened
    prepared = prepare_model_history(reopened, 3, policy)
    replaced = prepared.history[-2].content.output
    assert replaced["model_view"]["omitted_field"] == "matches"
    assert replaced["preview"]["spec_version"] == preview.spec_version
    assert replaced["preview"]["truncated"] and "matches" not in replaced["preview"]
    assert replaced["artifact"] == ref
    assert not prepared.new_decisions
    assert prepare_model_history(reopened, 3, policy) == prepared
    assert (
        prepare_model_history(
            reopened, 3, ToolResultViewPolicy(max_inline_utf8_bytes=80000)
        ).history
        == prepared.history
    )
    assert reopened.model_dump_json() == before


@pytest.mark.parametrize(
    "field,value", [("revision", "0" * 64), ("content_sha256", "0" * 64), ("file_bytes", 0)]
)
async def test_same_file_snapshot_output_rejects_inconsistent_observation(tmp_path, field, value):
    (tmp_path / "source.py").write_bytes(b"needle\nneedle\n")
    async with CodingToolRuntime(tmp_path, enable_grep_snapshots=True) as tools:
        output = (await execute(tools, "grep", query="needle")).output
    output["matches"][1][field] = value
    with pytest.raises(ValidationError, match="必须一致"):
        GrepSnapshotOutput.model_validate_json(json.dumps(output))
