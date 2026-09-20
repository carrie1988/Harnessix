from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.provider_suite_evidence import (
    build_provider_suite_evidence_manifest,
    distinct_evidence_roots,
    publish_provider_suite_evidence,
    validate_publishable_suite_evidence,
)
from harnessix.evals.report import (
    read_eval_suite_plan,
    read_eval_suite_report,
    write_eval_suite_plan,
    write_eval_suite_report,
)
from tests.evals.provider_suite_helpers import provider_suite_config, provider_suite_report


def test_provider_suite_evidence_rejects_content_fields_paths_and_private_fragments() -> None:
    validate_publishable_suite_evidence(
        {"safe": ["urn:harnessix:provider-suite", "relative/path.py"]}
    )
    with pytest.raises(KernelError, match="禁止字段"):
        validate_publishable_suite_evidence({"prompt": "private"})
    with pytest.raises(KernelError, match="绝对路径"):
        validate_publishable_suite_evidence({"value": "/private/run"})
    with pytest.raises(KernelError, match="绝对路径"):
        validate_publishable_suite_evidence({"value": "C:\\private\\run"})
    with pytest.raises(KernelError, match="私有运行身份"):
        validate_publishable_suite_evidence(
            {"value": "prefix/private-id/suffix"},
            forbidden_fragments=("private-id",),
        )


def test_provider_suite_private_and_public_roots_must_be_disjoint(tmp_path: Path) -> None:
    work, evidence = distinct_evidence_roots(tmp_path / "work", tmp_path / "evidence")
    assert work != evidence
    with pytest.raises(KernelError, match="相互分离"):
        distinct_evidence_roots(tmp_path / "work", tmp_path / "work" / "evidence")
    with pytest.raises(KernelError, match="相互分离"):
        distinct_evidence_roots(tmp_path / "work" / "nested", tmp_path / "work")


def test_provider_suite_evidence_manifest_requires_complete_matching_report(
    tmp_path: Path,
) -> None:
    config = provider_suite_config(tmp_path)
    report = provider_suite_report(config)

    manifest = build_provider_suite_evidence_manifest(config, report)

    assert manifest.scheduled_cases == 10 and manifest.scheduled_trials == 20
    assert manifest.passed_trials == 20 and manifest.cost_completeness == "complete"
    assert manifest.known_cost_amount == "0.0144"
    with pytest.raises(KernelError, match="报告或成本证据不完整"):
        build_provider_suite_evidence_manifest(
            config,
            report.model_copy(
                update={"plan": config.suite.plan.model_copy(update={"suite_version": 2})}
            ),
        )


def test_provider_suite_publishes_only_strict_low_sensitive_evidence(tmp_path: Path) -> None:
    config = provider_suite_config(tmp_path)
    report = provider_suite_report(config)
    private_root = Path(config.suite.work_root)
    private_root.mkdir(mode=0o700)
    write_eval_suite_plan(private_root / "suite-plan.json", config.suite.plan)
    write_eval_suite_report(private_root / "suite-report.json", report)
    evidence_root = tmp_path / "public-evidence"

    manifest = publish_provider_suite_evidence(config, evidence_root)

    assert sorted(path.name for path in evidence_root.iterdir()) == [
        "evidence-manifest.json",
        "suite-plan.json",
        "suite-report.json",
    ]
    assert read_eval_suite_plan(evidence_root / "suite-plan.json") == config.suite.plan
    assert read_eval_suite_report(evidence_root / "suite-report.json") == report
    assert manifest.report_sha256
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in evidence_root.iterdir())
