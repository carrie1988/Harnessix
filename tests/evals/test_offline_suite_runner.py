from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from scripts.recorded_task_pack import RecordedSolution, RecordedSolutionProvider
from scripts.run_engineering_offline_suite import (
    OfflineSuiteEvidenceManifest,
    _RecoveryFaults,
)


def test_recorded_review_answer_contains_required_finding_as_independent_token() -> None:
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    case = loaded.manifest.case("agents-secret-redaction-review")
    provider = RecordedSolutionProvider(
        RecordedSolution(case=case, before=b"before", content="after", mode=0o644),
        UUID("c2193b8d-cd1b-5f04-9b96-daa4cf65d595"),
    )

    answer = json.loads(provider._answer())

    assert "agents-api-key-plaintext" in answer["summary"].split()
    assert answer["changed_paths"] == ["src/agent_utils.py"]
    assert answer["tests"] == [{"profile": case.profile_id, "passed": True}]


def test_recovery_faults_fire_each_commit_window_exactly_once() -> None:
    faults = _RecoveryFaults()

    with pytest.raises(RuntimeError, match="case-evidence-crash"):
        faults("suite.after_case_evidence")
    faults("suite.after_case_evidence")
    with pytest.raises(RuntimeError, match="report-publication-crash"):
        faults("suite.after_report")
    faults("suite.after_report")
    assert faults.case_evidence_crashed and faults.report_crashed


def test_evidence_manifest_is_strict_and_digest_bound() -> None:
    payload = {
        "suite_id": UUID("79f70817-6fa0-5b4a-bf95-684b74f9fcb5"),
        "pack_id": "harnessix-engineering",
        "pack_version": 2,
        "pack_sha256": "a" * 64,
        "harnessix_revision": "b" * 40,
        "plan_fingerprint": "c" * 64,
        "report_sha256": "d" * 64,
        "scheduled_cases": 10,
        "scheduled_trials": 20,
        "passed_trials": 20,
        "provider_open_count": 20,
        "provider_request_count": 120,
        "case_evidence_recovery_verified": True,
        "report_publication_recovery_verified": True,
    }
    manifest = OfflineSuiteEvidenceManifest.model_validate(payload, strict=True)
    assert manifest.scheduled_trials == 20
    with pytest.raises(ValidationError):
        OfflineSuiteEvidenceManifest.model_validate({**payload, "unexpected": True}, strict=True)
    with pytest.raises(ValidationError):
        OfflineSuiteEvidenceManifest.model_validate(
            {**payload, "report_sha256": "not-a-digest"}, strict=True
        )
