#!/usr/bin/env python3
"""在固定Container中运行工程Task Pack完整离线Suite并发布脱敏证据。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field

from harnessix.domain.models import ContractModel
from harnessix.evals.execution_fs import ensure_private_directory
from harnessix.evals.report import (
    eval_suite_report_sha256,
    read_eval_suite_plan,
    read_eval_suite_report,
    write_eval_suite_plan,
    write_eval_suite_report,
)
from harnessix.evals.suite_contracts import CodingEvalSuiteReport
from harnessix.evals.suite_execution import run_coding_eval_suite
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteRunConfig
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_execution import TaskPackCaseExecutor
from harnessix.evals.task_pack_suite import build_task_pack_offline_suite_config

if __package__:
    from scripts.recorded_task_pack import RecordedProviderFactory
else:
    from recorded_task_pack import RecordedProviderFactory

_ROOT = Path(__file__).resolve().parents[1]
_SOLUTIONS = _ROOT / "benchmarks/taskpacks/harnessix-engineering-v1/solutions"
_SUITE_NAMESPACE = UUID("345b674d-7711-5774-81aa-f2a038527c4c")
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


class OfflineSuiteEvidenceManifest(ContractModel):
    """公开证据目录的低敏索引；不包含宿主路径或运行正文。"""

    spec_version: Literal["harnessix.offline-suite-evidence/v1"] = (
        "harnessix.offline-suite-evidence/v1"
    )
    suite_id: UUID
    pack_id: Literal["harnessix-engineering"]
    pack_version: Literal[1]
    pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    harnessix_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduled_cases: Literal[10]
    scheduled_trials: Literal[20]
    passed_trials: Literal[20]
    provider_open_count: Literal[20]
    provider_request_count: Literal[120]
    case_evidence_recovery_verified: Literal[True]
    report_publication_recovery_verified: Literal[True]


class _RecoveryFaults:
    def __init__(self) -> None:
        self.case_evidence_crashed = False
        self.report_crashed = False

    def __call__(self, point: str) -> None:
        if point == "suite.after_case_evidence" and not self.case_evidence_crashed:
            self.case_evidence_crashed = True
            raise RuntimeError("expected:case-evidence-crash")
        if point == "suite.after_report" and not self.report_crashed:
            self.report_crashed = True
            raise RuntimeError("expected:report-publication-crash")


def _executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise RuntimeError(f"缺少必需可执行文件：{name}")
    return Path(value).resolve(strict=True)


def _git_text(git: Path, *arguments: str) -> str:
    return subprocess.check_output(
        (str(git), *arguments),
        cwd=_ROOT,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=30,
    ).strip()


def _revision_time(git: Path, revision: str) -> datetime:
    return datetime.fromisoformat(_git_text(git, "show", "-s", "--format=%cI", revision))


def _require_fixed_images(config: CodingEvalSuiteRunConfig) -> None:
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    expected = {
        "HARNESSIX_TEST_PYTHON_IMAGE": {
            profile.image for profile in loaded.manifest.profiles if profile.language == "python"
        },
        "HARNESSIX_TEST_NODE_IMAGE": {
            profile.image
            for profile in loaded.manifest.profiles
            if profile.language == "javascript"
        },
    }
    if any(
        len(images) != 1 or os.environ.get(name) not in images for name, images in expected.items()
    ):
        raise RuntimeError("Task Pack固定镜像环境与Manifest不一致")
    if config.plan.environment.isolation != "fixed-container-no-network":
        raise RuntimeError("离线Suite隔离身份无效")


def _expect_crash(error: RuntimeError, marker: str) -> None:
    if str(error) != marker:
        raise error


async def _run_with_recovery(
    config: CodingEvalSuiteRunConfig,
    executor: TaskPackCaseExecutor,
    provider_factory: RecordedProviderFactory,
):
    faults = _RecoveryFaults()
    try:
        await run_coding_eval_suite(config, executor, fault=faults)
    except RuntimeError as error:
        _expect_crash(error, "expected:case-evidence-crash")
    else:
        raise AssertionError("完整Suite未命中Case证据崩溃窗口")
    if len(provider_factory.opened_run_ids) != 2:
        raise AssertionError("首个Case崩溃前必须精确完成两个Trial")

    try:
        await run_coding_eval_suite(config, executor, fault=faults)
    except RuntimeError as error:
        _expect_crash(error, "expected:report-publication-crash")
    else:
        raise AssertionError("完整Suite未命中报告发布崩溃窗口")

    async def forbidden(*_args):
        raise AssertionError("已发布Suite报告恢复不得再次执行Case")

    result = await run_coding_eval_suite(config, forbidden)
    if result.reason != "completed" or not result.report_published:
        raise AssertionError("完整Suite报告恢复未完成")
    return result, faults


def _validate_complete_report(
    config: CodingEvalSuiteRunConfig,
    provider_factory: RecordedProviderFactory,
) -> tuple[CodingEvalSuiteReport, int]:
    report = read_eval_suite_report(Path(config.work_root) / "suite-report.json")
    trials = tuple(trial for case in report.cases for trial in case.campaign.trials)
    transcripts = tuple(item for case in report.cases for item in case.transcripts)
    tests = tuple(item for case in report.cases for item in case.tests)
    request_count = sum(len(provider.requests) for provider in provider_factory.providers)
    expected_run_ids = {run_id for campaign in config.campaign_plans for run_id in campaign.run_ids}
    if (
        len(report.cases) != 10
        or len(trials) != 20
        or report.summary.repositories != 3
        or report.summary.scheduled_trials != 20
        or report.summary.passed_trials != 20
        or report.summary.tests_passed_trials != 20
        or report.summary.human_intervention_trials != 0
        or report.summary.model_attempts != 120
        or report.summary.input_tokens != 1200
        or report.summary.output_tokens != 600
        or report.summary.cost_completeness != "complete"
        or report.summary.known_cost_amount != "0"
        or any(item.human_intervention_count for item in transcripts)
        or any(item.automated_approval_decisions != 3 for item in transcripts)
        or any(item.outcome != "passed" for item in tests)
        or provider_factory.opened_run_ids != expected_run_ids
        or len(provider_factory.providers) != 20
        or request_count != 120
    ):
        raise AssertionError("完整离线Suite汇总、Trial或Provider计数不符合固定基线")
    return report, request_count


def _walk_publishable(value: object, forbidden_fragments: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        if _FORBIDDEN_KEYS.intersection(value):
            raise AssertionError("公开Suite证据包含禁止字段")
        for item in value.values():
            _walk_publishable(item, forbidden_fragments)
    elif isinstance(value, list):
        for item in value:
            _walk_publishable(item, forbidden_fragments)
    elif isinstance(value, str):
        if value.startswith("/") or _WINDOWS_ABSOLUTE.match(value):
            raise AssertionError("公开Suite证据包含宿主绝对路径")
        if any(fragment and fragment in value for fragment in forbidden_fragments):
            raise AssertionError("公开Suite证据包含私有运行身份")


def _write_manifest(path: Path, manifest: OfflineSuiteEvidenceManifest) -> None:
    body = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
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
                raise OSError("Suite证据清单写入失败")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _publish_evidence(
    config: CodingEvalSuiteRunConfig,
    evidence_root: Path,
    provider_factory: RecordedProviderFactory,
    request_count: int,
    faults: _RecoveryFaults,
) -> OfflineSuiteEvidenceManifest:
    work_root = Path(config.work_root)
    plan = read_eval_suite_plan(work_root / "suite-plan.json")
    report = read_eval_suite_report(work_root / "suite-report.json")
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    manifest = OfflineSuiteEvidenceManifest(
        suite_id=plan.suite_id,
        pack_id=loaded.manifest.pack_id,
        pack_version=loaded.manifest.pack_version,
        pack_sha256=loaded.manifest.pack_sha256,
        harnessix_revision=plan.environment.harnessix_revision,
        plan_fingerprint=plan.fingerprint,
        report_sha256=eval_suite_report_sha256(report),
        scheduled_cases=report.summary.scheduled_cases,
        scheduled_trials=report.summary.scheduled_trials,
        passed_trials=report.summary.passed_trials,
        provider_open_count=len(provider_factory.providers),
        provider_request_count=request_count,
        case_evidence_recovery_verified=faults.case_evidence_crashed,
        report_publication_recovery_verified=faults.report_crashed,
    )
    fragments = (str(_ROOT), str(work_root), str(_SOLUTIONS))
    for value in (
        plan.model_dump(mode="json"),
        report.model_dump(mode="json"),
        manifest.model_dump(mode="json"),
    ):
        _walk_publishable(value, fragments)
    ensure_private_directory(
        evidence_root,
        error_code="eval_suite_evidence_root_invalid",
        label="Suite公开证据目录",
    )
    write_eval_suite_plan(evidence_root / "suite-plan.json", plan)
    write_eval_suite_report(evidence_root / "suite-report.json", report)
    _write_manifest(evidence_root / "evidence-manifest.json", manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行Harnessix工程Task Pack完整离线Suite")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--suite-id", type=UUID)
    return parser


def _distinct_roots(work_root: Path, evidence_root: Path) -> tuple[Path, Path]:
    work = work_root.resolve(strict=False)
    evidence = evidence_root.resolve(strict=False)
    if work == evidence or work in evidence.parents or evidence in work.parents:
        raise RuntimeError("Suite私有运行目录与公开证据目录必须相互分离")
    return work, evidence


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    git = _executable("git")
    container = _executable("docker")
    revision = _git_text(git, "rev-parse", "HEAD")
    work_root, evidence_root = _distinct_roots(arguments.work_root, arguments.evidence_root)
    suite_id = arguments.suite_id or uuid5(_SUITE_NAMESPACE, f"harnessix-engineering:1:{revision}")
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    config = build_task_pack_offline_suite_config(
        loaded,
        suite_id=suite_id,
        work_root=work_root,
        harnessix_revision=revision,
        platform=sys.platform,
        created_at=_revision_time(git, revision),
    )
    _require_fixed_images(config)
    with tempfile.TemporaryDirectory(prefix="harnessix-recorded-oracles-") as temporary:
        oracle_root = Path(temporary) / "runs"
        oracle_root.mkdir(mode=0o700)
        provider_factory = RecordedProviderFactory(
            loaded,
            git_executable=git,
            oracle_root=oracle_root,
            solutions_root=_SOLUTIONS,
        )
        provider_factory.prepare()
        executor = TaskPackCaseExecutor(loaded, git, container, provider_factory)
        _, faults = asyncio.run(_run_with_recovery(config, executor, provider_factory))
        _, request_count = _validate_complete_report(config, provider_factory)
        manifest = _publish_evidence(
            config,
            evidence_root,
            provider_factory,
            request_count,
            faults,
        )
    print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
