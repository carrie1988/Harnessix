"""Harbor内置智能体的结果解析器：登记智能体阶段导出的补丁，用量沿用通用规则。

mini-swe-agent另按自身退出原因归因，口径与Harnessix一致（HarnessixResultParser，ADR 0123）：
达到步数上限是预算耗尽，墙钟上限是超时，模型服务临时故障是可重跑的基础设施失败。
Harnessix使用integrations.harnessix_eval.harbor_parser:HarnessixResultParser，它额外映射原生协议摘要。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evalkit.contracts import TrialStatus
from evalkit.harbor import AgentResult

MINI_TRAJECTORY_FILE = "mini-swe-agent.trajectory.json"
# mini-swe-agent 2.4.6 写入轨迹info.exit_status的退出原因，对应Harnessix协议摘要的同类终止。
MINI_EXIT_ATTRIBUTION: Mapping[str, tuple[TrialStatus | None, str]] = {
    "Submitted": (None, "completed"),
    "LimitsExceeded": ("system_error", "budget_exhausted"),
    "TimeExceeded": ("timeout", "deadline_exceeded"),
    "UserInterruption": ("system_error", "intervention_required"),
    "RepeatedFormatError": ("system_error", "agent_failed"),
}
# mini重试用尽后以异常类名作为退出原因；litellm的连接、超时、5xx与429对应产品SERVICE_FAILURE_CODES。
MINI_SERVICE_FAILURES = frozenset(
    {
        "APIConnectionError",
        "Timeout",
        "InternalServerError",
        "BadGatewayError",
        "ServiceUnavailableError",
        "MidStreamFallbackError",
        "RateLimitError",
        "CallerCredentialRateLimitError",
    }
)


class _PatchArtifactParser:
    agent_name = ""
    unique_refs: tuple[str, ...] = ()

    def parse(self, trial_dir: Path, result: Mapping[str, Any]) -> AgentResult:
        del result
        return AgentResult(artifact_files=_patch_files(trial_dir))


class OracleParser(_PatchArtifactParser):
    agent_name = "oracle"


class NopParser(_PatchArtifactParser):
    agent_name = "nop"


class MiniSweAgentParser(_PatchArtifactParser):
    agent_name = "mini-swe-agent"

    def parse(self, trial_dir: Path, result: Mapping[str, Any]) -> AgentResult:
        del result
        status, reason = mini_exit_status(trial_dir / "agent" / MINI_TRAJECTORY_FILE)
        return AgentResult(status=status, reason=reason, artifact_files=_patch_files(trial_dir))


def mini_exit_status(trajectory: Path) -> tuple[TrialStatus | None, str | None]:
    """轨迹缺失、读不出或没有退出原因时不作判断，沿用通用规则（Harbor异常决定）。"""
    exit_status = _exit_status(trajectory)
    if not exit_status:
        return None, None
    if exit_status in MINI_EXIT_ATTRIBUTION:
        return MINI_EXIT_ATTRIBUTION[exit_status]
    if exit_status in MINI_SERVICE_FAILURES:
        return "infra_error", "provider_unavailable"
    return "system_error", "agent_failed"


def _exit_status(trajectory: Path) -> str:
    if not trajectory.is_file():
        return ""
    try:
        info = json.loads(trajectory.read_text(encoding="utf-8")).get("info")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return ""
    value = info.get("exit_status") if isinstance(info, dict) else None
    return value if isinstance(value, str) else ""


def _patch_files(trial_dir: Path) -> dict[str, Path]:
    patch = trial_dir / "agent" / "patch.diff"
    return {"patch": patch} if patch.is_file() else {}
