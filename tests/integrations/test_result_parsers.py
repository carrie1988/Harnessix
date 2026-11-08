"""mini-swe-agent的终止归因与Harnessix同口径：同类终止经导入与报告得到相同状态、原因与判定。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evalkit.contracts import EvalRunReport, RunPlan, summarize
from evalkit.grading import build_report
from evalkit.harbor import HarborRewardGrader, import_harbor_job

from integrations.harbor.result_parsers import (
    MINI_SERVICE_FAILURES,
    MINI_TRAJECTORY_FILE,
    MiniSweAgentParser,
)
from integrations.harnessix_eval.harbor_parser import HarnessixResultParser
from tests.evalkit.builders import HARBOR_REWARD, Trial, harbor_job, harbor_plan
from tests.integrations.test_harnessix_eval import native

DONE = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
AGENT_EXIT = "NonZeroAgentExitCodeError"
MINI = "mini-swe-agent"
NO_TRAJECTORY = None
UNREADABLE = "<unreadable>"


def v2(plan: RunPlan) -> RunPlan:
    return plan.model_copy(update={"spec_version": "evalkit.run-plan/v2"})


def mini_report(
    tmp_path: Path, trials: list[tuple[Trial, str | None]], *, version2: bool = False
) -> EvalRunReport:
    """按E0b真实形状写agent/mini-swe-agent.trajectory.json的info.exit_status。"""
    job = harbor_job(tmp_path / "mini-job", [spec for spec, _ in trials], agent_name=MINI)
    for index, (_, exit_status) in enumerate(trials):
        agent = job / f"trial-{index}-0" / "agent"
        agent.mkdir()
        (agent / "patch.diff").write_text("diff --git a/x b/x\n")
        if exit_status == UNREADABLE:
            (agent / MINI_TRAJECTORY_FILE).write_text("{not json")
        elif exit_status is not NO_TRAJECTORY:
            info = {
                "exit_status": exit_status,
                "model_stats": {"instance_cost": 0.0, "api_calls": 100},
            }
            trajectory = {"info": info, "messages": [], "trajectory_format": "mini-swe-agent-1.1"}
            (agent / MINI_TRAJECTORY_FILE).write_text(json.dumps(trajectory))
    plan = harbor_plan(len(trials), system=MINI)
    plan = v2(plan) if version2 else plan
    imported = import_harbor_job(job, plan, "A", {MINI: MiniSweAgentParser()}, job.parent)
    return build_report(plan, imported, (HarborRewardGrader(HARBOR_REWARD),), job.parent, DONE)


def harnessix_report(tmp_path: Path, trials: list[Trial]) -> EvalRunReport:
    job = harbor_job(tmp_path / "harnessix-job", trials, agent_name="harnessix")
    plan = harbor_plan(len(trials), system="harnessix")
    imported = import_harbor_job(job, plan, "A", {"harnessix": HarnessixResultParser()}, job.parent)
    return build_report(plan, imported, (HarborRewardGrader(HARBOR_REWARD),), job.parent, DONE)


def outcome(report: EvalRunReport) -> tuple[str, str]:
    return report.trials[0].status, report.trials[0].reason


@pytest.mark.parametrize(
    ("exception", "exit_status", "expected"),
    [
        (None, "Submitted", ("completed", "completed")),
        (None, "LimitsExceeded", ("system_error", "budget_exhausted")),
        (None, "TimeExceeded", ("timeout", "deadline_exceeded")),
        (None, "UserInterruption", ("system_error", "intervention_required")),
        (None, "RepeatedFormatError", ("system_error", "agent_failed")),
        (AGENT_EXIT, "APIConnectionError", ("infra_error", "provider_unavailable")),
        (AGENT_EXIT, "Timeout", ("infra_error", "provider_unavailable")),
        (AGENT_EXIT, "ServiceUnavailableError", ("infra_error", "provider_unavailable")),
        (AGENT_EXIT, "RateLimitError", ("infra_error", "provider_unavailable")),
        (AGENT_EXIT, "ContextWindowExceededError", ("system_error", "agent_failed")),
        (AGENT_EXIT, "AuthenticationError", ("system_error", "agent_failed")),
        # 轨迹缺失、读不出或尚无退出原因：通用规则（Harbor异常决定），与改动前一致。
        (None, NO_TRAJECTORY, ("completed", "completed")),
        (None, UNREADABLE, ("completed", "completed")),
        (AGENT_EXIT, NO_TRAJECTORY, ("system_error", "agent_failed")),
        ("AgentTimeoutError", "", ("timeout", "deadline_exceeded")),
        ("EnvironmentStartTimeoutError", "", ("infra_error", "harbor_infrastructure_error")),
    ],
)
def test_mini_exit_status_is_attributed_like_harnessix(
    tmp_path: Path, exception: str | None, exit_status: str | None, expected: tuple[str, str]
) -> None:
    report = mini_report(tmp_path, [(Trial(reward=0.0, exception=exception), exit_status)])
    assert outcome(report) == expected


@pytest.mark.parametrize(
    ("exit_status", "exception", "harnessix"),
    [
        ("Submitted", None, native()),
        ("LimitsExceeded", None, native("failed", "budget")),
        ("TimeExceeded", None, native("deadline_exceeded")),
        ("UserInterruption", None, native("intervention_required")),
        (
            "APIConnectionError",
            AGENT_EXIT,
            native("failed", "provider", native_failure_code="provider_transport"),
        ),
        (
            "InternalServerError",
            AGENT_EXIT,
            native("failed", "provider", native_failure_code="provider_provider_internal"),
        ),
        (
            "RateLimitError",
            AGENT_EXIT,
            native("failed", "provider", native_failure_code="provider_rate_limit"),
        ),
    ],
)
def test_same_kind_of_stop_reads_the_same_for_both_systems(
    tmp_path: Path, exit_status: str, exception: str | None, harnessix
) -> None:
    mini_side = mini_report(tmp_path, [(Trial(reward=1.0, exception=exception), exit_status)])
    harnessix_exception = None if exit_status == "Submitted" else AGENT_EXIT
    harnessix_side = harnessix_report(
        tmp_path, [Trial(reward=1.0, exception=harnessix_exception, metadata=harnessix)]
    )
    assert outcome(mini_side) == outcome(harnessix_side)
    assert mini_side.summary == harnessix_side.summary


def test_v2_primary_scores_the_patch_and_stability_needs_a_normal_end(tmp_path: Path) -> None:
    """计划v2：步数用完但补丁通过时主指标记通过、稳定性指标记失败；服务故障两种口径都是invalid。"""
    report = mini_report(
        tmp_path,
        [
            (Trial(reward=1.0), "Submitted"),
            (Trial(reward=1.0), "LimitsExceeded"),
            (Trial(reward=0.0), "LimitsExceeded"),
            (Trial(reward=1.0, exception=AGENT_EXIT), "ServiceUnavailableError"),
        ],
        version2=True,
    )
    primary = report.summary[0]
    stability = summarize(report.plan, report.trials, require_normal_end=True)[0]
    assert (primary.passed, primary.failed, primary.invalid) == (2, 1, 1)
    assert (stability.passed, stability.failed, stability.invalid) == (1, 2, 1)


def test_budget_stop_keeps_the_patch_for_the_primary_grader(tmp_path: Path) -> None:
    report = mini_report(tmp_path, [(Trial(reward=1.0), "LimitsExceeded")])
    assert outcome(report) == ("system_error", "budget_exhausted")
    assert any(artifact.name == "patch" for artifact in report.trials[0].artifacts)


def test_service_failure_names_are_litellm_transport_5xx_and_429() -> None:
    """litellm的502/503类在构造时才设置status_code，按继承关系核对类名仍存在且属于服务方故障。"""
    litellm = pytest.importorskip("litellm")
    openai = pytest.importorskip("openai")
    errors = litellm.exceptions
    service = (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.RateLimitError,
        openai.InternalServerError,
        errors.ServiceUnavailableError,
        errors.BadGatewayError,
    )
    for name in MINI_SERVICE_FAILURES:
        assert issubclass(getattr(errors, name), service), name
    assert not issubclass(errors.ContextWindowExceededError, service)
    assert not issubclass(errors.AuthenticationError, service)
