from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

from harnessix.agent.models import TurnStatusV18
from harnessix.evals.campaign_contracts import (
    CodingEvalCampaignReport,
    CodingEvalCampaignTrial,
    summarize_campaign_trials,
)
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.provider_suite_contracts import CodingEvalProviderSuiteRunConfig
from harnessix.evals.suite import build_coding_eval_suite_report_from_cases
from harnessix.evals.suite_contracts import (
    CodingEvalSuiteCaseReport,
    CodingEvalSuiteReport,
    CodingEvalTranscriptEvidence,
    CodingEvalTrialTestEvidence,
)
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_suite import build_task_pack_suite_config
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.pricing import (
    BillingContext,
    FlatInputPrice,
    PriceSnapshot,
    content_digest,
)

MODEL = "qwen3-coder-plus-2025-09-23"
CREATED_AT = datetime(2026, 9, 20, 3, tzinfo=UTC)
SUITE_ID = UUID("8c8c625f-a884-51de-8574-c50b0dce4df8")
REVISION = "2983898358e6beb0dfb182dc80b5a85341c97d77"


def provider_suite_config(tmp_path: Path) -> CodingEvalProviderSuiteRunConfig:
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    price = PriceSnapshot(
        version="aliyun-bailian-2026-09-20-qwen3-coder-plus-32k",
        source_url="https://help.aliyun.com/zh/model-studio/qwen3-coder-plus",
        billing_provider="aliyun-bailian",
        model=MODEL,
        region="cn-beijing",
        service_tier="payg",
        inference_mode="non-thinking",
        currency="CNY",
        valid_from=datetime(2026, 9, 20, tzinfo=UTC),
        valid_until=datetime(2026, 9, 21, tzinfo=UTC),
        input_tokens_min=0,
        input_tokens_max=32_000,
        input_price=FlatInputPrice(per_million="4"),
        output_per_million="16",
    )
    billing = BillingContext(
        billing_provider="aliyun-bailian",
        region="cn-beijing",
        service_tier="payg",
        inference_mode="non-thinking",
    )
    suite = build_task_pack_suite_config(
        loaded,
        suite_id=SUITE_ID,
        work_root=tmp_path / "private-suite",
        environment=CodingEvalEnvironment(
            harnessix_revision=REVISION,
            provider="openai_chat",
            model=MODEL,
            platform="linux",
            isolation="fixed-container-checks-provider-network",
        ),
        price=price,
        billing_context=billing,
        fee_stop_amount="40",
        created_at=CREATED_AT,
    )
    return CodingEvalProviderSuiteRunConfig(
        suite=suite,
        pack_id=loaded.manifest.pack_id,
        pack_version=loaded.manifest.pack_version,
        pack_sha256=loaded.manifest.pack_sha256,
        source_root=str(tmp_path / "source"),
        git_executable="/usr/bin/git",
        container_engine="/usr/bin/docker",
        provider_config=OpenAIChatConfig(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=MODEL,
            api_key_env="DASHSCOPE_API_KEY",
            capabilities=ChatCapabilities(tool_calls=True, parallel_tool_calls=False),
            max_output_tokens=4096,
            timeout_seconds=900,
            io_timeout_seconds=60,
            max_attempts=1,
            retry_delay_seconds=0,
            output_token_parameter="max_tokens",
        ),
    )


def provider_suite_report(
    config: CodingEvalProviderSuiteRunConfig,
) -> CodingEvalSuiteReport:
    """构造只含低敏字段、可由正式合同重算的完整Suite报告。"""

    cases: list[CodingEvalSuiteCaseReport] = []
    for case_index, (case, campaign) in enumerate(
        zip(config.suite.plan.cases, config.suite.campaign_plans, strict=True)
    ):
        trials: list[CodingEvalCampaignTrial] = []
        transcripts: list[CodingEvalTranscriptEvidence] = []
        tests: list[CodingEvalTrialTestEvidence] = []
        for trial_index, run_id in enumerate(campaign.run_ids):
            started_at = campaign.created_at + timedelta(seconds=case_index * 10 + trial_index + 1)
            completed_at = started_at + timedelta(seconds=1)
            turn_id = uuid5(run_id, "provider-suite-turn")
            trial = CodingEvalCampaignTrial(
                run_id=run_id,
                eval_report_sha256=f"{case_index * 2 + trial_index + 1:064x}",
                turn_id=turn_id,
                turn_status=TurnStatusV18.COMPLETED,
                classification="passed",
                eval_outcome="passed",
                failure_categories=(),
                actual_models=(config.provider_config.model,),
                model_attempts=1,
                input_tokens=100,
                output_tokens=20,
                elapsed_seconds=1,
                cost_completeness="complete",
                known_cost_currency="CNY",
                known_cost_amount="0.00072",
                started_at=started_at,
                completed_at=completed_at,
            )
            trials.append(trial)
            transcripts.append(
                CodingEvalTranscriptEvidence(
                    run_id=run_id,
                    turn_id=turn_id,
                    turn_status=TurnStatusV18.COMPLETED,
                    transcript_sha256=f"{case_index * 2 + trial_index + 101:064x}",
                    completed_items=1,
                    model_steps=1,
                    model_attempts=1,
                    input_tokens=100,
                    output_tokens=20,
                    approval_requests=0,
                    automated_approval_decisions=0,
                    human_approval_interventions=0,
                    question_requests=0,
                    question_answers=0,
                    steering_messages=0,
                    manual_recovery_results=0,
                )
            )
            tests.append(
                CodingEvalTrialTestEvidence(
                    run_id=run_id,
                    outcome="passed",
                    total_checks=2,
                    passed_checks=2,
                )
            )
        trial_tuple = tuple(trials)
        campaign_report = CodingEvalCampaignReport(
            plan=campaign,
            plan_fingerprint=campaign.fingerprint,
            trials=trial_tuple,
            summary=summarize_campaign_trials(trial_tuple, "CNY"),
            completed_at=max(trial.completed_at for trial in trial_tuple),
        )
        cases.append(
            CodingEvalSuiteCaseReport(
                case_id=case.case_id,
                campaign=campaign_report,
                campaign_report_fingerprint=content_digest(campaign_report),
                transcripts=tuple(transcripts),
                tests=tuple(tests),
            )
        )
    return build_coding_eval_suite_report_from_cases(config.suite.plan, tuple(cases))
