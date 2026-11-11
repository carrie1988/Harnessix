"""v2/v3/v4诊断正文必须同时满足闭合DTO、原计划身份及原审计Hash。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.trusted_actions.agent_gateway_output import terminal_result
from tests.trusted_actions.test_agent_gateway import FixedOutput, agent_state
from tests.trusted_actions.test_process_failure_projection import ProcessExecutor, process_route
from tests.trusted_actions.test_success_projection_boundaries import output_reference

# v3每流多一个offset，上限从1 KiB升为8 KiB；v4再多一个removed_escape_bytes。
TAIL_FIELDS = {"v2": {}, "v3": {"offset": 0}, "v4": {"offset": 0, "removed_escape_bytes": 0}}
LIMITS = {"v2": 1024, "v3": 8192, "v4": 8192}


def preview_output(output: dict, version: str) -> dict:
    """按版本构造合法预览。"""
    public = dict(output)
    public["version"] = f"trusted-process-output/{version}"
    streams = {
        "stdout": {"text": "test diagnostic\n", "size_bytes": 16, "truncated": False},
        "stderr": {"text": "", "size_bytes": 0, "truncated": False},
    }
    for stream in streams.values():
        stream.update(TAIL_FIELDS[version])
    public["diagnostic_preview"] = streams
    return public


@pytest.mark.parametrize("version", ["v2", "v3", "v4"])
@pytest.mark.parametrize("delivery", ["owner", "inline"])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize(
    "case",
    ["valid", "missing", "size", "limit", "control", "extra", "truncated", "v1", "other-version"],
)
async def test_preview_requires_formal_contract_even_with_matching_audit(
    tmp_path, case, recovery, delivery, version
):
    class Returning(ProcessExecutor):
        async def execute(self, plan, arguments):
            raw = await super().execute(plan, arguments)
            public = preview_output(raw.output, version)
            preview = public["diagnostic_preview"]["stdout"]
            limit = LIMITS[version] + 1
            if case == "missing":
                del public["diagnostic_preview"]
            elif case == "size":
                preview["size_bytes"] = 17
            elif case == "limit":
                preview.update(text="x" * limit, size_bytes=limit)
            elif case == "control":
                preview["text"] = "x" * 15 + "\x00"
            elif case == "extra":
                public["diagnostic_preview"]["private"] = "not authorized"
            elif case == "truncated":
                preview["truncated"] = True
            elif case == "v1":
                public["version"] = "trusted-process-output/v1"
            elif case == "other-version":
                public["version"] = "trusted-process-output/" + ("v3" if version == "v2" else "v2")
            return raw.model_copy(update={"output": public})

    executor = Returning(False, "exited", "exited", 0)
    root = tmp_path / "workspace"
    actions, plans, audit, initial = process_route(root, executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        route = actions.status(initial.plan.execution.plan_id)
        projected = {**outcome.output, "artifact": output_reference(outcome.artifact_sha256)}
        output = FixedOutput(projected)
        state = SimpleNamespace(
            router=actions, outputs={route.plan.binding.tool: output} if delivery == "owner" else {}
        )
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        original_events = actions.events(route.plan.execution.plan_id)
        current = (
            outcome.model_copy(update={"output": None})
            if recovery and delivery == "owner"
            else outcome
        )
        if case == "valid":
            result = await terminal_result(
                state,
                route,
                thread,
                turn,
                call,
                current,
                CancelToken(),
                origin="recovery" if recovery else "execution",
            )
            assert result.outcome == "succeeded"
            assert result.output == (projected if delivery == "owner" else outcome.output)
        else:
            with pytest.raises(KernelError) as caught:
                await terminal_result(
                    state,
                    route,
                    thread,
                    turn,
                    call,
                    current,
                    CancelToken(),
                    origin="recovery" if recovery else "execution",
                )
            assert caught.value.code == "trusted_action_output_mismatch"
        assert actions.events(route.plan.execution.plan_id) == original_events
        assert executor.calls == 1
    finally:
        plans.close()
        audit.close()


@pytest.mark.parametrize("version", ["v2", "v3", "v4"])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("delivery", ["owner", "inline"])
@pytest.mark.parametrize(
    "case", ["complete", "truncated", "null", "incomplete", "v1", "timeout", "unknown"]
)
async def test_nonzero_feedback_only_consumes_verified_visible_preview(
    tmp_path, recovery, delivery, case, version
):
    class Returning(ProcessExecutor):
        async def execute(self, plan, arguments):
            raw = await super().execute(plan, arguments)
            if case == "v1":
                return raw
            public = preview_output(raw.output, version)
            offset = TAIL_FIELDS[version]
            if case == "truncated":
                public["diagnostic_preview"]["stdout"] = {
                    "text": "test",
                    **offset,
                    "size_bytes": 4,
                    "truncated": True,
                }
            elif case == "null":
                public["diagnostic_preview"]["stdout"] = {
                    "text": None,
                    **offset,
                    "size_bytes": 0,
                    "truncated": True,
                }
            elif case == "incomplete":
                public["complete"] = False
                public["stdout"]["eof"] = False
            return raw.model_copy(update={"output": public})

    executor = Returning(
        False,
        "unknown" if case == "unknown" else "exited",
        "host_lost" if case == "unknown" else "timeout" if case == "timeout" else "exited",
        None if case == "unknown" else -15 if case == "timeout" else 1,
    )
    root = tmp_path / "workspace"
    actions, plans, audit, initial = process_route(root, executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        route = actions.status(initial.plan.execution.plan_id)
        projected = {**outcome.output, "artifact": output_reference(outcome.artifact_sha256)}
        state = SimpleNamespace(
            router=actions,
            outputs={route.plan.binding.tool: FixedOutput(projected)}
            if delivery == "owner"
            else {},
        )
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        events = actions.events(initial.plan.execution.plan_id)
        current = (
            outcome.model_copy(update={"output": None})
            if recovery and delivery == "owner"
            else outcome
        )
        result = await terminal_result(
            state,
            route,
            thread,
            turn,
            call,
            current,
            CancelToken(),
            origin="recovery" if recovery else "execution",
        )
        assert result.outcome == outcome.kind and result.error.code == outcome.error_code
        assert result.error.retryable is False
        assert result.output == (projected if delivery == "owner" else outcome.output)
        assert result.trusted_action.artifact_sha256 == outcome.artifact_sha256
        assert result.trusted_action.origin == ("recovery" if recovery else "execution")
        message = result.error.message
        if delivery == "owner" and case == "timeout":
            # 评测实例：测试命令挂到时限被终止（exited/-15/timeout），同一条命令里排在后面的恢复
            # 步骤从未执行，原结果只说“Trusted Action未成功”，模型两步后才发现修改不在工作区。
            # 反馈明说命令没有跑完、复合命令的后续部分没有执行；只陈述通用事实，不识别具体命令。
            assert message.isascii()
            assert "did not finish" in message and "never ran" in message
            assert "non-zero status" not in message and "test diagnostic" not in message
        elif delivery == "owner" and case not in {"v1", "unknown"}:
            assert message.isascii()
            assert "exited with a non-zero status; this is not a launch failure" in message
            assert "test diagnostic" not in message
            if case == "complete":
                assert "do not read the artifact for the same output" in message
            else:
                assert "read the referenced artifact in bounded pages" in message
                assert "do not read" not in message
        else:
            assert message == "Trusted Action未成功；详细事实请查询审计记录"
        assert actions.events(initial.plan.execution.plan_id) == events and executor.calls == 1
    finally:
        plans.close()
        audit.close()
