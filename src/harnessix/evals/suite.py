"""从单任务Campaign与持久Turn构建多仓库Eval Suite报告。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    ApprovalRequestContent,
    ItemStatus,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    QuestionAnswerContent,
    QuestionRequestContent,
    TextContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatusV18,
)
from harnessix.domain.models import ActionStatus
from harnessix.evals.campaign import (
    CompletedCodingEvalTrial,
    build_coding_eval_campaign_report,
)
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.contracts import CodingEvalTask
from harnessix.evals.suite_contracts import (
    CodingEvalSuiteCasePlan,
    CodingEvalSuiteCaseReport,
    CodingEvalSuitePlan,
    CodingEvalSuiteReport,
    CodingEvalTranscriptEvidence,
    CodingEvalTrialTestEvidence,
    summarize_suite_cases,
)
from harnessix.models.pricing import content_digest

_AUTOMATED_APPROVAL_ACTORS = frozenset({"harnessix-eval-runner"})


@dataclass(frozen=True, slots=True)
class CompletedCodingEvalSuiteCase:
    """一个Suite Case的固定任务、Campaign计划和完整试验证据。"""

    case_id: str
    task: CodingEvalTask
    campaign_plan: CodingEvalCampaignPlan
    trials: tuple[CompletedCodingEvalTrial, ...]


def build_transcript_evidence(
    run_id: UUID,
    turn: Turn,
    *,
    automated_approval_actors: frozenset[str] = _AUTOMATED_APPROVAL_ACTORS,
) -> CodingEvalTranscriptEvidence:
    """只发布结构计数和摘要，不复制Prompt、回答、参数、输出或路径。"""

    if turn.status not in TERMINAL_TURNS:
        raise KernelError("eval_transcript_status_invalid", "Eval Transcript不是冻结终态")
    try:
        status = TurnStatusV18(turn.status.value)
    except ValueError:
        raise KernelError("eval_transcript_status_invalid", "Eval Transcript不是冻结终态") from None
    approval_requests = 0
    automated_decisions = 0
    human_approvals = 0
    question_requests = 0
    question_answers = 0
    user_messages = 0
    manual_calls: set[UUID] = set()
    approval_types = (
        ApprovalRequestContent,
        PatchApprovalRequestContent,
        PatchBatchApprovalRequestContent,
        ProcessApprovalRequestContent,
        TrustedActionApprovalRequestContent,
    )
    for item in turn.items:
        content = item.content
        if isinstance(content, approval_types):
            approval_requests += 1
            if content.decision is None:
                human_approvals += 1
            elif content.decision.actor in automated_approval_actors:
                automated_decisions += 1
            else:
                human_approvals += 1
        elif isinstance(content, QuestionRequestContent):
            question_requests += 1
        elif isinstance(content, QuestionAnswerContent):
            question_answers += 1
        elif (
            item.status is ItemStatus.COMPLETED
            and isinstance(content, TextContent)
            and content.kind == "user_message"
        ):
            user_messages += 1
        elif isinstance(content, ToolResultContent):
            if (
                content.trusted_action is not None
                and content.trusted_action.state == "manual_intervention"
            ) or (
                content.process is not None
                and content.process.status is ActionStatus.MANUAL_INTERVENTION
            ):
                manual_calls.add(content.call_id)
        elif (
            isinstance(content, ProcessActionStateContent)
            and content.effect.status is ActionStatus.MANUAL_INTERVENTION
        ):
            manual_calls.add(content.call_id)
    return CodingEvalTranscriptEvidence(
        run_id=run_id,
        turn_id=turn.turn_id,
        turn_status=status,
        transcript_sha256=content_digest(turn),
        completed_items=sum(item.status is ItemStatus.COMPLETED for item in turn.items),
        model_steps=turn.model_steps,
        model_attempts=len(turn.accounted_attempts),
        input_tokens=turn.usage.input_tokens,
        output_tokens=turn.usage.output_tokens,
        approval_requests=approval_requests,
        automated_approval_decisions=automated_decisions,
        human_approval_interventions=human_approvals,
        question_requests=question_requests,
        question_answers=question_answers,
        steering_messages=max(0, user_messages - 1),
        manual_recovery_results=len(manual_calls),
    )


def build_coding_eval_suite_case_report(
    expected: CodingEvalSuiteCasePlan,
    completed: CompletedCodingEvalSuiteCase,
) -> CodingEvalSuiteCaseReport:
    """把单个Case的完整内部证据投影为可持久化脱敏报告。"""

    task = completed.task
    campaign_plan = completed.campaign_plan
    if (
        completed.case_id != expected.case_id
        or task.task_id != expected.task_id
        or task.task_version != expected.task_version
        or task.fingerprint != expected.task_fingerprint
        or task.repository != expected.repository
        or campaign_plan.fingerprint != expected.campaign_plan_fingerprint
        or campaign_plan.task_id != task.task_id
        or campaign_plan.task_version != task.task_version
        or campaign_plan.task_fingerprint != task.fingerprint
    ):
        raise KernelError("eval_suite_case_mismatch", "Suite Case任务或Campaign身份不匹配")
    campaign = build_coding_eval_campaign_report(campaign_plan, completed.trials)
    transcripts = tuple(
        build_transcript_evidence(trial.state.run_id, trial.turn) for trial in completed.trials
    )
    expected_checks = tuple(sorted((*task.behavior_checks, *task.regression_checks)))
    tests = tuple(
        CodingEvalTrialTestEvidence(
            run_id=trial.state.run_id,
            outcome=(
                "passed"
                if tuple(item.check_id for item in trial.report.final_observations)
                == expected_checks
                and all(item.passed for item in trial.report.final_observations)
                else "failed"
            ),
            total_checks=len(expected_checks),
            passed_checks=sum(
                item.passed
                for item in trial.report.final_observations
                if item.check_id in expected_checks
            ),
        )
        for trial in completed.trials
    )
    return CodingEvalSuiteCaseReport(
        case_id=expected.case_id,
        campaign=campaign,
        campaign_report_fingerprint=content_digest(campaign),
        transcripts=transcripts,
        tests=tests,
    )


def build_coding_eval_suite_report_from_cases(
    plan: CodingEvalSuitePlan,
    cases: tuple[CodingEvalSuiteCaseReport, ...],
) -> CodingEvalSuiteReport:
    """从按计划持久化的完整Case报告重建Suite报告。"""

    try:
        plan = CodingEvalSuitePlan.model_validate_json(plan.model_dump_json(), strict=True)
    except ValueError:
        raise KernelError("eval_suite_plan_invalid", "Eval Suite计划无效") from None
    if len(cases) != len(plan.cases):
        raise KernelError("eval_suite_incomplete", "Eval Suite尚未具备全部Case证据")
    try:
        return CodingEvalSuiteReport(
            plan=plan,
            plan_fingerprint=plan.fingerprint,
            cases=cases,
            summary=summarize_suite_cases(plan, cases),
            completed_at=max(case.campaign.completed_at for case in cases),
        )
    except ValueError:
        raise KernelError("eval_suite_evidence_invalid", "Eval Suite证据无法一致聚合") from None


def build_coding_eval_suite_report(
    plan: CodingEvalSuitePlan,
    completed: tuple[CompletedCodingEvalSuiteCase, ...],
) -> CodingEvalSuiteReport:
    """完整证据才发布Suite；缺项、越序、跨任务或跨环境均失败关闭。"""

    try:
        plan = CodingEvalSuitePlan.model_validate_json(plan.model_dump_json(), strict=True)
    except ValueError:
        raise KernelError("eval_suite_plan_invalid", "Eval Suite计划无效") from None
    if len(completed) != len(plan.cases):
        raise KernelError("eval_suite_incomplete", "Eval Suite尚未具备全部Case证据")
    cases = tuple(
        build_coding_eval_suite_case_report(expected, actual)
        for expected, actual in zip(plan.cases, completed, strict=True)
    )
    return build_coding_eval_suite_report_from_cases(plan, cases)
