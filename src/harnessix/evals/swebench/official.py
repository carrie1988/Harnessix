"""固定调用官方 SWE-bench v5 评测器并回收可审计结果。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.files import hash_regular, read_regular
from harnessix.evals.swebench.predictions import (
    SweBenchPrediction,
    predictions_sha256,
    validate_predictions,
)

_MAX_RESULT_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_RUN_ID_PREFIX = "harnessix"
_COMPLETED_ARTIFACTS = (
    "eval.sh",
    "patch.diff",
    "report.json",
    "run_instance.log",
    "test_output.txt",
)


class SweBenchOfficialResult(ContractModel):
    """SWE-bench revision 02e7a74 的 results.json v2。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    total_instances: int = Field(ge=1)
    submitted_instances: int = Field(ge=0)
    completed_instances: int = Field(ge=0)
    resolved_instances: int = Field(ge=0)
    unresolved_instances: int = Field(ge=0)
    infra_failure_instances: int = Field(ge=0)
    ambiguous_failure_instances: int = Field(ge=0)
    empty_patch_instances: int = Field(ge=0)
    error_instances: int = Field(ge=0)
    completed_ids: tuple[str, ...]
    incomplete_ids: tuple[str, ...]
    empty_patch_ids: tuple[str, ...]
    submitted_ids: tuple[str, ...]
    resolved_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    infra_failure_ids: tuple[str, ...]
    ambiguous_failure_ids: tuple[str, ...]
    failure_reasons: dict[str, str]
    error_ids: tuple[str, ...]
    schema_version: int = Field(ge=2)
    unstopped_instances: int | None = Field(default=None, ge=0)
    unstopped_containers: tuple[str, ...] | None = None
    unremoved_images: tuple[str, ...] | None = None

    @field_validator(
        "completed_ids",
        "incomplete_ids",
        "empty_patch_ids",
        "submitted_ids",
        "resolved_ids",
        "unresolved_ids",
        "infra_failure_ids",
        "ambiguous_failure_ids",
        "error_ids",
    )
    @classmethod
    def canonical_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if list(values) != sorted(set(values)):
            raise ValueError("SWE-bench 结果 ID 必须唯一并按字节序排序")
        return values

    @model_validator(mode="after")
    def counts_match_ids(self) -> Self:
        pairs = (
            (self.submitted_instances, self.submitted_ids),
            (self.completed_instances, self.completed_ids),
            (self.resolved_instances, self.resolved_ids),
            (self.unresolved_instances, self.unresolved_ids),
            (self.infra_failure_instances, self.infra_failure_ids),
            (self.ambiguous_failure_instances, self.ambiguous_failure_ids),
            (self.empty_patch_instances, self.empty_patch_ids),
            (self.error_instances, self.error_ids),
        )
        if any(count != len(identities) for count, identities in pairs):
            raise ValueError("SWE-bench 结果计数与 ID 集合不一致")
        submitted = set(self.submitted_ids)
        completed = set(self.completed_ids)
        resolved = set(self.resolved_ids)
        unresolved = set(self.unresolved_ids)
        empty = set(self.empty_patch_ids)
        errors = set(self.error_ids)
        if not (
            completed <= submitted
            and resolved <= completed
            and unresolved <= completed
            and not resolved & unresolved
            and empty <= submitted
            and not empty & (completed | errors)
            and errors <= submitted
            and submitted == completed | errors | empty
            and set(self.infra_failure_ids) <= unresolved | errors
            and set(self.ambiguous_failure_ids)
            <= unresolved | errors
        ):
            raise ValueError("SWE-bench 结果集合关系无效")
        return self


class SweBenchEvaluationSummary(ContractModel):
    """把官方结果绑定到预注册计划与 prediction 内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    spec_version: str = "harnessix.swebench-evaluation-summary/v1"
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,95}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predictions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_results_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_files: int = Field(ge=0)
    submitted_cases: int = Field(ge=1)
    completed_cases: int = Field(ge=0)
    resolved_cases: int = Field(ge=0)
    unresolved_cases: int = Field(ge=0)
    infrastructure_failures: int = Field(ge=0)
    ambiguous_failures: int = Field(ge=0)
    error_cases: int = Field(ge=0)
    empty_patch_cases: int = Field(ge=0)


def evaluation_run_id(
    plan: SweBenchPreRegistrationPlan,
    predictions: tuple[SweBenchPrediction, ...],
) -> str:
    checked = validate_predictions(plan, predictions)
    return f"{_RUN_ID_PREFIX}-{plan.plan_sha256[:12]}-{predictions_sha256(checked)[:12]}"


def build_official_evaluation_command(
    *,
    executable: Path,
    swebench_source: Path,
    git_executable: Path,
    plan: SweBenchPreRegistrationPlan,
    predictions_path: Path,
    predictions: tuple[SweBenchPrediction, ...],
    workers: int,
    timeout_seconds: int,
    task_repo: Path,
) -> tuple[str, ...]:
    """构造 argv 而非 shell 字符串；run id 绑定计划和补丁以隔离官方缓存。"""

    binary = _resolved_executable(executable)
    source = _resolved_directory(swebench_source)
    tasks = _resolved_directory(task_repo)
    git = _resolved_executable(git_executable)
    if source not in binary.parents:
        raise KernelError(
            "swebench_executable_source_mismatch",
            "SWE-bench 可执行文件不属于固定源码环境",
        )
    _require_git_revision(git, source, plan.swebench_revision, "SWE-bench 评测器")
    _require_git_revision(git, tasks, plan.task_repo_revision, "SWE-bench Task Repo")
    prediction_file = _resolved_regular(predictions_path)
    if not 1 <= workers <= 32 or not 60 <= timeout_seconds <= 7200:
        raise KernelError("swebench_evaluator_config_invalid", "SWE-bench 评测资源参数无效")
    checked = validate_predictions(plan, predictions)
    command = [
        str(binary),
        "eval",
        plan.dataset_name,
        "--predictions",
        str(prediction_file),
        "--run-id",
        evaluation_run_id(plan, checked),
        "--split",
        plan.split,
        "--workers",
        str(workers),
        "--timeout",
        str(timeout_seconds),
    ]
    for case in plan.cases:
        command.extend(("--instance", case.instance_id))
    command.extend(("--task-repo", str(tasks)))
    return tuple(command)


def run_official_evaluation(
    *,
    command: tuple[str, ...],
    work_root: Path,
    allow_network: bool,
) -> None:
    """显式联网门禁后运行官方评测器；输出留在其原生日志目录。"""

    if not allow_network:
        raise KernelError("swebench_network_not_enabled", "SWE-bench 官方评测未显式允许联网")
    root = _resolved_directory(work_root)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise KernelError("swebench_evaluator_failed", "SWE-bench 官方评测器执行失败") from None
    if result.returncode != 0:
        raise KernelError("swebench_evaluator_failed", "SWE-bench 官方评测器返回失败")


def import_official_result(
    *,
    work_root: Path,
    plan: SweBenchPreRegistrationPlan,
    predictions: tuple[SweBenchPrediction, ...],
) -> tuple[SweBenchOfficialResult, SweBenchEvaluationSummary]:
    """读取官方 results.json，并拒绝 Case、计划或 prediction 绑定漂移。"""

    checked = validate_predictions(plan, predictions)
    run_id = evaluation_run_id(plan, checked)
    result_path = (
        _resolved_directory(work_root) / "logs" / "evaluation" / run_id / "results.json"
    )
    body = read_regular(
        result_path,
        _MAX_RESULT_BYTES,
        error_code="swebench_results_invalid",
        label="SWE-bench 官方结果",
    )
    try:
        result = SweBenchOfficialResult.model_validate_json(body, strict=True)
    except ValueError:
        raise KernelError("swebench_results_invalid", "SWE-bench 官方结果无效") from None
    planned = tuple(sorted(case.instance_id for case in plan.cases))
    if (
        result.total_instances != len(plan.cases)
        or result.submitted_instances != len(plan.cases)
        or result.submitted_ids != planned
        or result.incomplete_ids
    ):
        raise KernelError("swebench_results_scope_mismatch", "SWE-bench 官方结果超出预注册范围")
    artifact_files, artifacts_sha256 = _verify_official_artifacts(
        result_path.parent,
        plan,
        checked,
        result,
    )
    summary = SweBenchEvaluationSummary(
        run_id=run_id,
        plan_sha256=plan.plan_sha256,
        predictions_sha256=predictions_sha256(checked),
        official_results_sha256=hashlib.sha256(body).hexdigest(),
        artifacts_sha256=artifacts_sha256,
        artifact_files=artifact_files,
        submitted_cases=result.submitted_instances,
        completed_cases=result.completed_instances,
        resolved_cases=result.resolved_instances,
        unresolved_cases=result.unresolved_instances,
        infrastructure_failures=result.infra_failure_instances,
        ambiguous_failures=result.ambiguous_failure_instances,
        error_cases=result.error_instances,
        empty_patch_cases=result.empty_patch_instances,
    )
    return result, summary


def _verify_official_artifacts(
    run_root: Path,
    plan: SweBenchPreRegistrationPlan,
    predictions: tuple[SweBenchPrediction, ...],
    result: SweBenchOfficialResult,
) -> tuple[int, str]:
    """核对完成 Case 的原生日志闭包，并只向公开摘要暴露文件摘要。"""

    prediction_by_id = {item.instance_id: item for item in predictions}
    model_directory = plan.model_name.replace("/", "__")
    records: list[dict[str, object]] = []
    for instance_id in result.submitted_ids:
        instance_root = run_root / model_directory / instance_id
        required = _COMPLETED_ARTIFACTS if instance_id in result.completed_ids else ()
        for name in _COMPLETED_ARTIFACTS:
            path = instance_root / name
            if name in required and not path.exists():
                raise KernelError(
                    "swebench_artifacts_incomplete",
                    "SWE-bench 完成 Case 缺少官方原生日志",
                )
            if not path.exists():
                continue
            size, revision = hash_regular(
                path,
                _MAX_ARTIFACT_BYTES,
                error_code="swebench_artifacts_invalid",
                label="SWE-bench 官方日志",
            )
            if name == "patch.diff":
                expected = prediction_by_id[instance_id].model_patch.encode("utf-8")
                if size != len(expected) or revision != hashlib.sha256(expected).hexdigest():
                    raise KernelError(
                        "swebench_artifacts_mismatch",
                        "SWE-bench 官方 patch 与冻结 prediction 不一致",
                    )
            records.append(
                {
                    "path": f"{model_directory}/{instance_id}/{name}",
                    "bytes": size,
                    "sha256": revision,
                }
            )
    body = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(records), hashlib.sha256(body).hexdigest()


def _resolved_executable(path: Path) -> Path:
    resolved = _resolved_regular(path)
    if not os.access(resolved, os.X_OK):
        raise KernelError("swebench_executable_invalid", "SWE-bench 可执行文件无效")
    return resolved


def _resolved_regular(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise OSError
        return resolved
    except (OSError, RuntimeError):
        raise KernelError("swebench_path_invalid", "SWE-bench 文件路径无效") from None


def _resolved_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        if not stat.S_ISDIR(resolved.stat().st_mode):
            raise OSError
        return resolved
    except (OSError, RuntimeError):
        raise KernelError("swebench_path_invalid", "SWE-bench 目录路径无效") from None


def _require_git_revision(
    git: Path,
    repository: Path,
    expected: str,
    label: str,
) -> None:
    try:
        result = subprocess.run(
            (
                str(git),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "rev-parse",
                "HEAD",
            ),
            cwd=repository,
            env={
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise KernelError("swebench_revision_invalid", f"{label} Revision 无法读取") from None
    if result.returncode != 0 or result.stdout.strip() != expected.encode("ascii"):
        raise KernelError("swebench_revision_mismatch", f"{label} Revision 与预注册计划不一致")
