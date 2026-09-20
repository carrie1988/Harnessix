"""构建并原子发布受控真实Provider Suite低敏证据。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.evals.execution_fs import ensure_private_directory
from harnessix.evals.provider_suite_contracts import (
    CodingEvalProviderSuiteEvidenceManifest,
    CodingEvalProviderSuiteRunConfig,
)
from harnessix.evals.report import (
    eval_suite_report_sha256,
    read_eval_suite_plan,
    read_eval_suite_report,
    write_eval_suite_plan,
    write_eval_suite_report,
)
from harnessix.evals.suite_contracts import CodingEvalSuiteReport

_FORBIDDEN_KEYS = {
    "arguments",
    "diff",
    "prompt",
    "response",
    "secret",
    "tool_output",
    "work_root",
    "workspace",
}
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def distinct_evidence_roots(work_root: Path, evidence_root: Path) -> tuple[Path, Path]:
    """拒绝把公开证据写入私有运行目录或其父子目录。"""

    work = work_root.resolve(strict=False)
    evidence = evidence_root.resolve(strict=False)
    if work == evidence or work in evidence.parents or evidence in work.parents:
        raise KernelError(
            "eval_suite_evidence_scope_invalid",
            "Suite私有运行目录与公开证据目录必须相互分离",
        )
    return work, evidence


def validate_publishable_suite_evidence(
    value: object,
    *,
    forbidden_fragments: tuple[str, ...] = (),
) -> None:
    """递归拒绝正文型字段、宿主绝对路径和调用方指定的私有标识。"""

    if isinstance(value, dict):
        if _FORBIDDEN_KEYS.intersection(value):
            raise KernelError("eval_suite_evidence_sensitive", "公开Suite证据包含禁止字段")
        for item in value.values():
            validate_publishable_suite_evidence(
                item,
                forbidden_fragments=forbidden_fragments,
            )
    elif isinstance(value, list):
        for item in value:
            validate_publishable_suite_evidence(
                item,
                forbidden_fragments=forbidden_fragments,
            )
    elif isinstance(value, str):
        if value.startswith("/") or _WINDOWS_ABSOLUTE.match(value):
            raise KernelError("eval_suite_evidence_sensitive", "公开Suite证据包含宿主绝对路径")
        if any(fragment and fragment in value for fragment in forbidden_fragments):
            raise KernelError("eval_suite_evidence_sensitive", "公开Suite证据包含私有运行身份")


def build_provider_suite_evidence_manifest(
    config: CodingEvalProviderSuiteRunConfig,
    report: CodingEvalSuiteReport,
) -> CodingEvalProviderSuiteEvidenceManifest:
    """从完整可重算报告构造白名单索引，成本不完整时失败关闭。"""

    if report.plan != config.suite.plan or report.summary.cost_completeness != "complete":
        raise KernelError(
            "eval_provider_suite_evidence_incomplete",
            "真实Provider Suite报告或成本证据不完整",
        )
    summary = report.summary
    if summary.known_cost_currency is None or summary.known_cost_amount is None:
        raise KernelError(
            "eval_provider_suite_evidence_incomplete",
            "真实Provider Suite报告缺少完整成本",
        )
    first = config.suite.campaign_plans[0].price
    return CodingEvalProviderSuiteEvidenceManifest(
        suite_id=report.plan.suite_id,
        pack_id=config.pack_id,
        pack_version=config.pack_version,
        pack_sha256=config.pack_sha256,
        harnessix_revision=report.plan.environment.harnessix_revision,
        model=report.plan.environment.model,
        region=first.region,
        price_sha256=first.digest,
        pricing_source_url=first.source_url,
        plan_fingerprint=report.plan_fingerprint,
        report_sha256=eval_suite_report_sha256(report),
        scheduled_cases=summary.scheduled_cases,
        scheduled_trials=summary.scheduled_trials,
        passed_trials=summary.passed_trials,
        tests_passed_trials=summary.tests_passed_trials,
        human_intervention_trials=summary.human_intervention_trials,
        model_attempts=summary.model_attempts,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        known_cost_currency=summary.known_cost_currency,
        known_cost_amount=summary.known_cost_amount,
        fee_stop_currency=config.suite.fee_stop_currency,
        fee_stop_amount=config.suite.fee_stop_amount,
        completed_at=report.completed_at,
    )


def _write_manifest(path: Path, manifest: CodingEvalProviderSuiteEvidenceManifest) -> None:
    body = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(body) > 64 * 1024:
        raise KernelError("eval_suite_evidence_too_large", "Suite证据清单超过大小上限")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if path.is_symlink():
            raise OSError
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise KernelError("eval_suite_evidence_write_failed", "Suite证据清单写入失败") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass


def publish_provider_suite_evidence(
    config: CodingEvalProviderSuiteRunConfig,
    evidence_root: Path,
) -> CodingEvalProviderSuiteEvidenceManifest:
    """从私有完成事实发布Plan、Report与白名单Manifest，不复制Session或Workspace。"""

    work_root, public_root = distinct_evidence_roots(Path(config.suite.work_root), evidence_root)
    plan = read_eval_suite_plan(work_root / "suite-plan.json")
    report = read_eval_suite_report(work_root / "suite-report.json")
    if plan != config.suite.plan or report.plan != plan:
        raise KernelError(
            "eval_provider_suite_evidence_mismatch", "真实Provider Suite证据与配置不一致"
        )
    manifest = build_provider_suite_evidence_manifest(config, report)
    fragments = (
        config.source_root,
        config.suite.work_root,
        config.git_executable,
        config.container_engine,
    )
    for value in (
        plan.model_dump(mode="json"),
        report.model_dump(mode="json"),
        manifest.model_dump(mode="json"),
    ):
        validate_publishable_suite_evidence(value, forbidden_fragments=fragments)
    ensure_private_directory(
        public_root,
        error_code="eval_suite_evidence_root_invalid",
        label="Suite公开证据目录",
    )
    write_eval_suite_plan(public_root / "suite-plan.json", plan)
    write_eval_suite_report(public_root / "suite-report.json", report)
    _write_manifest(public_root / "evidence-manifest.json", manifest)
    return manifest
