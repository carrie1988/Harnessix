"""Harnessix在Harbor结果中自报的协议摘要 → evalkit通用状态与system_refs。

只读取适配器写入agent_result.metadata.harnessix的结构化字段，不读日志、提示词或回复正文。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evalkit.contracts import TrialStatus, Usage
from evalkit.harbor import AgentResult

# 协议运行器导出的本Turn公开事件（逐步记录来源）。
EVENTS_ARTIFACT = "harnessix_events"
EVENTS_FILE = "harnessix-events.jsonl"
_REFS = (
    "thread_id",
    "turn_id",
    "protocol_reason",
    "runner_error",
    "turn_status",
    "native_failure_category",
    "native_failure_code",
    "observed_model_steps",
    "protocol_evidence_complete",
    "turn_terminal_confirmed",
    "tool_results_succeeded",
    "final_assistant_sha256",
)


def _object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Harnessix协议摘要必须为对象")
    return value


def _ref(value: Any) -> str:
    return value if isinstance(value, str) else str(value).lower()


def _status(
    native: dict[str, Any], agent_exited_abnormally: bool
) -> tuple[TrialStatus | None, str]:
    """沿用ADR 0110的归因：未完成却无智能体异常，说明协议证据不完整，判为基础设施问题。"""
    reason = native.get("protocol_reason")
    category = native.get("native_failure_category")
    if reason == "completed":
        return None, "completed"
    if reason == "server_unavailable":
        return "infra_error", "server_unavailable"
    if reason == "deadline_exceeded":
        return "timeout", "deadline_exceeded"
    if reason == "intervention_required":
        return "system_error", "intervention_required"
    if category == "provider":
        return "system_error", "provider_failed"
    if category == "budget":
        return "system_error", "budget_exhausted"
    if agent_exited_abnormally:
        return "system_error", "agent_failed"
    return "infra_error", "protocol_incomplete"


class HarnessixResultParser:
    agent_name = "harnessix"
    unique_refs = ("turn_id",)

    def parse(self, trial_dir: Path, result: Mapping[str, Any]) -> AgentResult:
        agent = _object(result.get("agent_result"))
        native = _object(_object(agent.get("metadata")).get("harnessix"))
        if not native:
            return AgentResult(status="infra_error", reason="protocol_missing")
        if native.get("trial_id") != result.get("id"):
            raise ValueError("Harnessix协议摘要绑定了其他试次")
        exception = _object(result.get("exception_info")).get("exception_type")
        status, reason = _status(native, exception is not None)
        tokens = (
            native.get("observed_input_tokens", agent.get("n_input_tokens")),
            native.get("observed_output_tokens", agent.get("n_output_tokens")),
        )
        usage = (
            Usage()
            if all(value is None for value in tokens)
            else Usage(
                completeness="partial",
                model_calls=native.get("observed_model_steps"),
                input_tokens=tokens[0],
                output_tokens=tokens[1],
            )
        )
        files = {
            name: trial_dir / "agent" / filename
            for name, filename in (("patch", "patch.diff"), (EVENTS_ARTIFACT, EVENTS_FILE))
        }
        return AgentResult(
            status=status,
            reason=reason,
            usage=usage,
            artifact_files={name: path for name, path in files.items() if path.is_file()},
            system_refs={k: _ref(native[k]) for k in _REFS if native.get(k) is not None},
        )
