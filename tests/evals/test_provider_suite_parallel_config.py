"""显式批量模型调用配置；旧串行合同、评分与恢复身份保持独立。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolResultContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.evals.provider_suite_contracts import CodingEvalProviderSuiteRunConfig
from harnessix.evals.suite_execution import run_coding_eval_suite, suite_execution_fingerprint
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteCaseRunResult
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from scripts import create_engineering_provider_suite_config as generator
from tests.evals.provider_suite_helpers import provider_suite_config
from tests.models.wire import WireStream, chunk, frame, response

V1 = "harnessix.coding-eval-provider-suite-run-config/v1"
V2 = "harnessix.coding-eval-provider-suite-run-config/v2"


def parallel_config(original, *, version=V2, enabled=True):
    payload = original.model_dump(mode="python")
    payload["spec_version"] = version
    payload["provider_config"]["capabilities"]["parallel_tool_calls"] = enabled
    return CodingEvalProviderSuiteRunConfig.model_validate(payload, strict=True)


def test_parallel_requires_explicit_version_and_preserves_old_serial_wire(tmp_path):
    original = provider_suite_config(tmp_path)
    original_wire = original.model_dump_json()
    with pytest.raises(ValidationError, match="串行"):
        parallel_config(original, version=V1)

    candidate = parallel_config(original)
    assert candidate.suite == original.suite
    assert candidate.pack_sha256 == original.pack_sha256
    assert candidate.provider_config.max_attempts == 1
    assert candidate.provider_config.max_output_tokens == original.provider_config.max_output_tokens
    assert candidate.provider_config.timeout_seconds == original.provider_config.timeout_seconds
    assert candidate.provider_config.model == original.provider_config.model
    assert candidate.fingerprint != original.fingerprint
    assert suite_execution_fingerprint(candidate.suite, candidate.fingerprint) != (
        suite_execution_fingerprint(original.suite, original.fingerprint)
    )
    restored = CodingEvalProviderSuiteRunConfig.model_validate_json(original_wire)
    assert restored.model_dump_json() == original_wire
    assert CodingEvalProviderSuiteRunConfig.model_validate_json(candidate.model_dump_json()) == (
        candidate
    )


@pytest.mark.parametrize(
    "field,value", [("max_attempts", 2), ("max_output_tokens", 4097), ("retry_delay_seconds", 1)]
)
def test_parallel_version_preserves_attempt_and_output_limits(tmp_path, field, value):
    payload = parallel_config(provider_suite_config(tmp_path)).model_dump(mode="python")
    payload["provider_config"][field] = value
    with pytest.raises(ValidationError):
        CodingEvalProviderSuiteRunConfig.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "parallel_enabled,error",
    [(True, "并行工具能力要求工具调用能力"), (False, "原生工具调用")],
)
def test_parallel_version_still_requires_native_tools(tmp_path, parallel_enabled, error):
    payload = parallel_config(provider_suite_config(tmp_path)).model_dump(mode="python")
    payload["provider_config"]["capabilities"]["tool_calls"] = False
    payload["provider_config"]["capabilities"]["parallel_tool_calls"] = parallel_enabled
    with pytest.raises(ValidationError, match=error):
        CodingEvalProviderSuiteRunConfig.model_validate(payload, strict=True)


@pytest.mark.parametrize("first_version,first_enabled", [(V1, False), (V2, True)])
async def test_persisted_suite_rejects_parallel_contract_drift(
    tmp_path, first_version, first_enabled
):
    original = parallel_config(
        provider_suite_config(tmp_path), version=first_version, enabled=first_enabled
    )
    executions = []

    async def stop(case, campaign, case_root, cancel):
        executions.append(case.case_id)
        return CodingEvalSuiteCaseRunResult(case_id=case.case_id, reason="evidence_missing")

    stopped = await run_coding_eval_suite(
        original.suite, stop, execution_binding_sha256=original.fingerprint
    )
    assert stopped.reason == "evidence_missing" and len(executions) == 1
    state = Path(original.suite.work_root) / "suite-state.json"
    before = state.read_bytes()
    changed = parallel_config(original, enabled=not first_enabled)
    with pytest.raises(KernelError) as caught:
        await run_coding_eval_suite(
            changed.suite, stop, resume=True, execution_binding_sha256=changed.fingerprint
        )
    assert caught.value.code == "eval_suite_execution_mismatch"
    assert len(executions) == 1 and state.read_bytes() == before


@pytest.mark.parametrize("flag", [False, True])
def test_generator_explicitly_selects_version_without_starting_execution(
    tmp_path, monkeypatch, capsys, flag
):
    output = tmp_path / "suite.json"
    monkeypatch.setattr(generator, "_executable", lambda _: tmp_path / "fixture-executable")
    monkeypatch.setattr(generator, "_require_clean_revision", lambda _: "a" * 40)
    args = ["--output", str(output), "--work-root", str(tmp_path / "work")]
    if flag:
        args.append("--parallel-tool-calls")
    generator.main(args)
    config = CodingEvalProviderSuiteRunConfig.model_validate_json(output.read_text())
    assert config.spec_version == (V2 if flag else V1)
    assert config.provider_config.capabilities.parallel_tool_calls is flag
    assert len(config.suite.plan.cases) == 10
    assert sum(len(p.run_ids) for p in config.suite.campaign_plans) == 20
    assert not (tmp_path / "work").exists()
    result = json.loads(capsys.readouterr().out)
    assert result["config_fingerprint"] == config.fingerprint


async def test_v2_native_group_reads_two_files_before_next_model_request(tmp_path, monkeypatch):
    """实际Adapter和文件工具消费同组提案；离线响应不证明模型会主动选择批量。"""
    config = parallel_config(provider_suite_config(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("a.py", "b.py"):
        (workspace / name).write_text(f"# {name}\n")
    requests = []
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["parallel_tool_calls"] is True
        if len(requests) == 1:
            calls = [
                {
                    "index": index,
                    "id": f"read-{index}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": name})},
                }
                for index, name in enumerate(("a.py", "b.py"))
            ]
            frames = [chunk({"tool_calls": calls}), chunk(finish="tool_calls"), chunk(usage=True)]
        else:
            assert len(requests) == 2
            results = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
            assert [r["output"]["path"] for r in results] == ["a.py", "b.py"]
            assert all(r["outcome"] == "succeeded" for r in results)
            frames = [chunk({"content": "已读取两文件"}), chunk(finish="stop"), chunk(usage=True)]
        for item in frames:
            item["model"] = config.provider_config.model
        return response(WireStream([*(frame(item) for item in frames), b"data: [DONE]\n\n"]))

    async with (
        OpenAIChatProvider(
            config.provider_config,
            api_key="synthetic-fixture-credential",
            transport=httpx.MockTransport(handle),
        ) as provider,
        CodingToolRuntime(workspace) as tools,
        AgentRuntime(SQLiteSessionStore(tmp_path / "session.db"), provider, tools) as runtime,
    ):
        thread = await runtime.create_thread(str(workspace))
        turn = await runtime.run_turn(thread.thread_id, "读取a.py和b.py", request_id="parallel")
    assert turn.status == "completed" and len(requests) == 2
    results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert len(results) == 2 and all(r.outcome == "succeeded" for r in results)
