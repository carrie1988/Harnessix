import json

import httpx
import httpx2
import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.batch_diff import SQLiteBatchDiffPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation
from tests.models import anthropic_wire as aw
from tests.models import wire as ow
from tests.patches.kernel_batch_helpers import approval_of, decide
from tests.patches.test_kernel_patch import results
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case


@pytest.mark.parametrize("archive", [False, True])
@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
async def test_sdk_reads_batch_reopen_approve_write_readback_and_private_wire(
    group_case, tmp_path, monkeypatch, vendor, archive
):
    source, factory, copy, groups, _ = group_case
    original = snapshot(source.root)
    monkeypatch.setenv("HARNESSIX_BATCH_FIXTURE_KEY", "offline-fixture-not-a-credential")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    requests, streams = [], []

    def outputs(body):
        if vendor == "openai":
            return [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        return [
            json.loads(c["content"])
            for m in body["messages"]
            if isinstance(m["content"], list)
            for c in m["content"]
            if c["type"] == "tool_result"
        ]

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        index, previous = len(requests), outputs(body)
        tools = body["tools"]
        names = {t["function"]["name"] if vendor == "openai" else t["name"] for t in tools}
        assert tool_alias("apply_patch_batch") in names and tool_alias("apply_patch") not in names
        if index in (1, 2, 4, 5):
            name, args = "read_file", {"path": PATHS[1 if index in (2, 5) else 0]}
            if index == 4:
                assert previous[-1]["outcome"] == "succeeded"
                assert previous[-1]["output"]["effect"] == "applied"
                assert len(previous[-1]["output"]["files"]) == 2
        elif index == 3:
            assert all(o["output"]["text"] == "before\r\n" for o in previous)
            name, args = (
                "apply_patch_batch",
                {
                    "files": [
                        {
                            "path": path,
                            "expected_revision": previous[i]["output"]["revision"],
                            "edits": [{"old_text": "before", "new_text": "after"}],
                        }
                        for i, path in enumerate(PATHS[:2])
                    ]
                },
            )
        elif archive and index == 6:
            reference = previous[2]["diff_artifact"]
            assert reference["complete"] and previous[2]["output"]["effect"] == "applied"
            name, args = (
                "read_artifact",
                {
                    "artifact_id": reference["artifact_id"],
                    "offset": 1,
                    "limit": 1,
                },
            )
        elif archive and index == 7:
            page = previous[-1]["output"]
            assert json.loads(page["text"])["path"] == PATHS[0]
            assert page["next_offset"] == 2
            name, args = None, None
        else:
            assert index == 6 and all(o["output"]["text"] == "after\r\n" for o in previous[-2:])
            name, args = None, None
        wire = ow if vendor == "openai" else aw
        if name is None:
            frames = wire.text_frames()
        elif vendor == "openai":
            frames = [
                ow.frame(
                    ow.chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call-{index}",
                                    "type": "function",
                                    "function": {
                                        "name": tool_alias(name),
                                        "arguments": json.dumps(args),
                                    },
                                }
                            ]
                        }
                    )
                ),
                ow.frame(ow.chunk(finish="tool_calls")),
                ow.frame(ow.chunk(usage=True)),
                b"data: [DONE]\n\n",
            ]
        else:
            frames = [
                aw.start(),
                aw.frame(
                    "content_block_start",
                    index=0,
                    content_block={
                        "type": "tool_use",
                        "id": f"toolu_{index}",
                        "name": tool_alias(name),
                        "input": {},
                    },
                ),
                aw.frame(
                    "content_block_delta",
                    index=0,
                    delta={"type": "input_json_delta", "partial_json": json.dumps(args)},
                ),
                aw.frame("content_block_stop", index=0),
                *aw.stop("tool_use"),
            ]
        stream = wire.WireStream(frames)
        streams.append(stream)
        return wire.response(stream)

    def provider():
        if vendor == "openai":
            return OpenAIChatProvider(
                OpenAIChatConfig(
                    model="test-model",
                    base_url="https://provider.invalid/v1",
                    api_key_env="HARNESSIX_BATCH_FIXTURE_KEY",
                    max_attempts=1,
                ),
                transport=httpx.MockTransport(handle),
            )
        return AnthropicProvider(
            AnthropicConfig(
                model="test-model",
                base_url="https://provider.invalid",
                api_key_env="HARNESSIX_BATCH_FIXTURE_KEY",
                max_attempts=1,
            ),
            transport=httpx2.MockTransport(handle),
        )

    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store) if archive else None
    async with (
        ManagedPatchBatchBridge(copy) as bridge,
        CodingToolRuntime(copy.workspace.root, artifacts=artifacts) as reads,
        provider() as model,
    ):
        async with AgentRuntime(
            store,
            model,
            scoped_tools=reads,
            patch_batches=bridge,
            artifacts=artifacts,
            batch_diffs=SQLiteBatchDiffPublisher(artifacts, bridge) if archive else None,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(
                thread.thread_id, "修改两个文件并读回", request_id="sdk-batch"
            )
            assert waiting.status == TurnStatus.WAITING_APPROVAL and len(requests) == 3
            request = approval_of(waiting)
            assert groups.get(request.plan.backend.batch_id, ReadOperation()).decision is None
    copy.close()
    with factory.open(request.plan.backend.workspace_id) as reopened:
        async with (
            ManagedPatchBatchBridge(reopened) as bridge,
            CodingToolRuntime(reopened.workspace.root, artifacts=artifacts) as reads,
            provider() as model,
        ):
            async with AgentRuntime(
                store,
                model,
                scoped_tools=reads,
                patch_batches=bridge,
                artifacts=artifacts,
                batch_diffs=SQLiteBatchDiffPublisher(artifacts, bridge) if archive else None,
            ) as runtime:
                assert len(requests) == 3
                await decide(runtime, thread.thread_id, waiting)
                assert (
                    ManagedPatchBatches(reopened)
                    .get(request.plan.backend.batch_id, ReadOperation())
                    .decision
                    is None
                )
                turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                assert turn.status == TurnStatus.COMPLETED
    assert len(requests) == (7 if archive else 6) and all(s.closed for s in streams)
    assert len(results(turn)) == (6 if archive else 5)
    assert (approval_of(turn).diff_artifact is not None) == archive
    assert results(turn)[2].patch_batch.execution.effect == "applied"
    public = json.dumps(requests)
    for private in (
        str(request.plan.backend.batch_id),
        str(request.plan.backend.workspace_id),
        request.plan.approval_fingerprint,
        request.plan.backend.approval_fingerprint,
        request.plan.backend.request_id,
        '"patch_batch":',
        '"patch":',
        "kernel-managed-patch-batch/v1",
        *(str(m.plan_id) for m in request.plan.backend.members),
    ):
        assert private not in public
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
    assert snapshot(source.root) == original
