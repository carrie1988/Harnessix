"""Harnessix侧适配：Harbor原生协议摘要的归因，以及Suite报告回填为evalkit运行报告。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from evalkit.contracts import EvalRunReport
from evalkit.errors import EvalkitError
from evalkit.files import read_model
from evalkit.grading import build_report
from evalkit.harbor import HarborRewardGrader, import_harbor_job
from evalkit.langfuse import build_export

from harnessix.evals.campaign_contracts import summarize_campaign_trials
from harnessix.evals.report import write_eval_suite_report
from harnessix.evals.suite import build_coding_eval_suite_report
from harnessix.evals.suite_contracts import CodingEvalSuiteReport, summarize_suite_cases
from harnessix.models.pricing import content_digest
from integrations.harnessix_eval.__main__ import main as harnessix_eval_main
from integrations.harnessix_eval.harbor_parser import HarnessixResultParser
from integrations.harnessix_eval.suite import suite_run_report
from tests.evalkit.builders import HARBOR_REWARD, Trial, change_result, harbor_job, harbor_plan
from tests.evals.test_suite import _suite

PARSERS = {"harnessix": HarnessixResultParser()}
DONE = datetime(2026, 11, 5, 11, 0, tzinfo=UTC)


def suite_report(*, failed: bool = False) -> CodingEvalSuiteReport:
    plan, cases = _suite(profile_evidence=not failed)
    return build_coding_eval_suite_report(plan, cases)


def native(reason: str = "completed", category: str | None = None, **extra: Any):
    def build(trial_id: str) -> dict[str, Any]:
        return {
            "harnessix": {
                "trial_id": trial_id,
                "thread_id": str(uuid4()),
                "turn_id": str(uuid4()),
                "protocol_reason": reason,
                "turn_status": "completed" if reason == "completed" else "failed",
                "native_failure_category": category,
                "protocol_evidence_complete": True,
                "observed_input_tokens": 200,
                "observed_output_tokens": 40,
                **extra,
            }
        }

    return build


AGENT_EXIT = "NonZeroAgentExitCodeError"
MATRIX = [
    Trial(reward=1.0, metadata=native()),
    Trial(reward=0.0, metadata=native()),
    Trial(reward=0.0, exception=AGENT_EXIT, metadata=native("deadline_exceeded")),
    Trial(reward=0.0, exception=AGENT_EXIT, metadata=native("intervention_required")),
    Trial(reward=0.0, exception=AGENT_EXIT, metadata=native("failed", "provider")),
    None,
]


def harbor_report(job: Path, tasks: int) -> EvalRunReport:
    plan = harbor_plan(tasks, system="harnessix")
    trials = import_harbor_job(job, plan, "A", PARSERS, job.parent)
    return build_report(plan, trials, (HarborRewardGrader(HARBOR_REWARD),), job.parent, DONE)


# ---------------------------------------------------------------- Harbor协议摘要


def test_native_matrix_keeps_attribution_denominator_and_unknown_billing(tmp_path: Path) -> None:
    report = harbor_report(harbor_job(tmp_path / "job", MATRIX, agent_name="harnessix"), 6)
    assert [(t.status, t.reason) for t in report.trials] == [
        ("completed", "completed"),
        ("completed", "completed"),
        ("timeout", "deadline_exceeded"),
        ("system_error", "intervention_required"),
        ("system_error", "provider_failed"),
        ("missing", "missing_result"),
    ]
    summary = report.summary[0]
    assert (summary.scheduled, summary.passed, summary.failed, summary.invalid) == (6, 1, 4, 1)
    assert report.trials[0].system_refs["protocol_evidence_complete"] == "true"
    assert report.trials[0].usage.input_tokens == 200
    assert all(t.cost.completeness == "unknown" for t in report.trials)
    assert "PRIVATE-EXCEPTION" not in report.model_dump_json()
    export = build_export(report)
    assert export == build_export(report)
    assert "generation" not in export.model_dump_json()


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (Trial(metadata=None), ("infra_error", "protocol_missing")),
        (Trial(metadata=native("failed", "tool")), ("infra_error", "protocol_incomplete")),
        (
            Trial(exception=AGENT_EXIT, metadata=native("failed", "tool")),
            ("system_error", "agent_failed"),
        ),
        (Trial(exception=AGENT_EXIT, metadata=native()), ("system_error", "agent_failed")),
        (Trial(metadata=native("failed", "budget")), ("system_error", "budget_exhausted")),
        # 产品未能启动不是智能体失败：环境或安装问题，判无效。
        (
            Trial(exception=AGENT_EXIT, metadata=native("server_unavailable")),
            ("infra_error", "server_unavailable"),
        ),
        # 服务方临时故障判基础设施失败（ADR 0123）；其他Provider失败仍算系统失败。
        (
            Trial(
                exception=AGENT_EXIT,
                metadata=native(
                    "failed", "provider", native_failure_code="provider_provider_internal"
                ),
            ),
            ("infra_error", "provider_unavailable"),
        ),
        (
            Trial(
                exception=AGENT_EXIT,
                metadata=native(
                    "failed", "provider", native_failure_code="provider_authentication"
                ),
            ),
            ("system_error", "provider_failed"),
        ),
    ],
)
def test_incomplete_protocol_evidence_is_never_a_pass(
    tmp_path: Path, spec: Trial, expected: tuple[str, str]
) -> None:
    report = harbor_report(harbor_job(tmp_path / "job", [spec], agent_name="harnessix"), 1)
    assert (report.trials[0].status, report.trials[0].reason) == expected
    assert report.summary[0].passed == 0


@pytest.mark.parametrize("field", ["trial_id", "duplicate_turn"])
def test_native_identity_must_bind_to_its_own_trial(tmp_path: Path, field: str) -> None:
    job = harbor_job(tmp_path / "job", [Trial(metadata=native())] * 2, agent_name="harnessix")
    if field == "trial_id":
        native_of = lambda d: d["agent_result"]["metadata"]["harnessix"]  # noqa: E731
        change_result(job, lambda d: native_of(d).update(trial_id="x"), "trial-0-0")
    else:
        turn = json.loads((job / "trial-0-0/result.json").read_text())["agent_result"]["metadata"]
        change_result(
            job,
            lambda d: d["agent_result"]["metadata"]["harnessix"].update(
                turn_id=turn["harnessix"]["turn_id"]
            ),
            "trial-1-0",
        )
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(2, system="harnessix"), "A", PARSERS, tmp_path)
    assert error.value.code == "harbor_source_invalid"


# ---------------------------------------------------------------- Suite回填


def test_suite_backfill_matches_every_original_verdict() -> None:
    source = suite_report(failed=True)
    report = suite_run_report(source)
    assert report.plan.provenance == "backfilled"
    assert report.plan.created_at == source.plan.created_at
    assert len(report.trials) == source.summary.scheduled_trials == 10
    assert report.summary[0].passed == source.summary.passed_trials
    assert report == suite_run_report(source)
    assert report.plan.source_refs["suite_plan_fingerprint"] == source.plan_fingerprint


def test_suite_backfill_pairs_items_across_suites_with_new_traces() -> None:
    source = suite_report()
    plan = source.plan.model_copy(update={"suite_id": uuid4()})
    changed = source.model_copy(update={"plan": plan, "plan_fingerprint": plan.fingerprint})

    def spans(report: CodingEvalSuiteReport) -> list[dict[str, Any]]:
        raw = json.loads(build_export(suite_run_report(report)).model_dump_json())
        return raw["resource_spans"][0]["scopeSpans"][0]["spans"]

    def value(span: dict[str, Any], key: str) -> Any:
        return next(a["value"] for a in span["attributes"] if a["key"] == key)

    paired = ("langfuse.experiment.dataset.id", "langfuse.experiment.item.id")
    for before, after in zip(spans(source), spans(changed), strict=True):
        assert before["traceId"] != after["traceId"]
        assert all(value(before, key) == value(after, key) for key in paired)


def test_suite_unknown_cost_stays_unknown_and_cny_is_not_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = suite_report()
    cases = []
    for case in source.cases:
        trials = tuple(
            trial.model_copy(
                update={
                    "cost_completeness": "unknown",
                    "known_cost_currency": None,
                    "known_cost_amount": None,
                }
            )
            for trial in case.campaign.trials
        )
        campaign = case.campaign.model_copy(
            update={"trials": trials, "summary": summarize_campaign_trials(trials, "USD")}
        )
        cases.append(
            case.model_copy(
                update={
                    "campaign": campaign,
                    "campaign_report_fingerprint": content_digest(campaign),
                }
            )
        )
    unknown = source.model_copy(
        update={"cases": tuple(cases), "summary": summarize_suite_cases(source.plan, tuple(cases))}
    )
    assert all(t.cost.amount is None for t in suite_run_report(unknown).trials)

    from tests.evals import test_suite

    price = test_suite.price().model_copy(update={"currency": "CNY"})
    monkeypatch.setattr(test_suite, "price", lambda: price)
    converted = suite_run_report(suite_report())
    assert {t.cost.currency for t in converted.trials} == {"CNY"}


def test_suite_export_uses_field_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.evals import test_suite

    environment = test_suite.campaign_environment().model_copy(
        update={"isolation": "PRIVATE-DIAGNOSTIC-MUST-NOT-EXPORT"}
    )
    monkeypatch.setattr(test_suite, "campaign_environment", lambda: environment)
    body = build_export(suite_run_report(suite_report())).model_dump_json()
    assert "PRIVATE-DIAGNOSTIC" not in body
    assert "https://github.com" not in body
    assert "baseline_tree" not in body


def test_from_suite_cli_writes_private_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, output = tmp_path / "suite.json", tmp_path / "run-report.json"
    write_eval_suite_report(source, suite_report())
    before = source.read_bytes()
    harnessix_eval_main(["from-suite", "--suite-report", str(source), "--output", str(output)])
    assert json.loads(capsys.readouterr().out)["reason"] == "converted"
    assert output.stat().st_mode & 0o777 == 0o600
    report = read_model(output, EvalRunReport, 1024 * 1024, require_private=True)
    assert report.summary[0].scheduled == 10
    with pytest.raises(SystemExit):
        harnessix_eval_main(["from-suite", "--suite-report", str(source), "--output", str(source)])
    assert source.read_bytes() == before


def test_parser_service_failure_codes_match_the_product() -> None:
    from harnessix.agent.provider_stop import SERVICE_FAILURE_CODES as PRODUCT_CODES
    from integrations.harnessix_eval.harbor_parser import SERVICE_FAILURE_CODES

    assert SERVICE_FAILURE_CODES == PRODUCT_CODES
