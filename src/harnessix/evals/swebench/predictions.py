"""把隔离工作区变更导出为官方 SWE-bench prediction JSONL。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from pydantic import ConfigDict, Field, field_validator

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.files import read_regular, write_atomic

_MAX_PATCH_BYTES = 8 * 1024 * 1024
_MAX_PREDICTIONS_BYTES = 64 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 120
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


class SweBenchPrediction(ContractModel):
    """SWE-bench 官方评测器接受的最小 prediction 结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    instance_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$")
    model_name_or_path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
    model_patch: str = Field(max_length=_MAX_PATCH_BYTES)

    @field_validator("model_patch")
    @classmethod
    def valid_unified_diff(cls, value: str) -> str:
        if value == "":
            return value
        if "\x00" in value or not value.startswith("diff --git a/"):
            raise ValueError("SWE-bench prediction 必须为空或为 Git unified diff")
        if "\ndiff --git a/" not in f"\n{value}":
            raise ValueError("SWE-bench prediction 缺少 Git diff 文件头")
        return value


def predictions_sha256(predictions: tuple[SweBenchPrediction, ...]) -> str:
    body = _prediction_jsonl(predictions)
    return hashlib.sha256(body).hexdigest()


def _prediction_jsonl(predictions: tuple[SweBenchPrediction, ...]) -> bytes:
    return b"".join(
        (
            json.dumps(
                prediction.model_dump(mode="json", warnings="error"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        for prediction in predictions
    )


def validate_predictions(
    plan: SweBenchPreRegistrationPlan,
    predictions: tuple[SweBenchPrediction, ...],
) -> tuple[SweBenchPrediction, ...]:
    """拒绝缺失、越界、重复或模型身份漂移的 prediction 集合。"""

    planned_ids = tuple(case.instance_id for case in plan.cases)
    observed_ids = tuple(prediction.instance_id for prediction in predictions)
    if observed_ids != planned_ids:
        raise KernelError(
            "swebench_predictions_scope_mismatch",
            "SWE-bench predictions 必须按预注册顺序完整覆盖全部 Case",
        )
    if any(prediction.model_name_or_path != plan.model_name for prediction in predictions):
        raise KernelError(
            "swebench_prediction_model_mismatch",
            "SWE-bench prediction 模型身份与预注册计划不一致",
        )
    return predictions


def write_predictions(
    path: Path,
    plan: SweBenchPreRegistrationPlan,
    predictions: tuple[SweBenchPrediction, ...],
) -> str:
    """原子写入官方 JSONL，并返回绑定评测 run id 的内容摘要。"""

    checked = validate_predictions(plan, predictions)
    body = _prediction_jsonl(checked)
    if len(body) > _MAX_PREDICTIONS_BYTES:
        raise KernelError("swebench_predictions_too_large", "SWE-bench predictions 超过大小上限")
    write_atomic(
        path,
        body,
        error_code="swebench_predictions_write_failed",
        label="SWE-bench predictions",
    )
    return hashlib.sha256(body).hexdigest()


def read_predictions(
    path: Path,
    plan: SweBenchPreRegistrationPlan,
) -> tuple[SweBenchPrediction, ...]:
    """从普通文件读取完整 prediction 集合，不跟随符号链接。"""

    body = read_regular(
        path,
        _MAX_PREDICTIONS_BYTES,
        error_code="swebench_predictions_invalid",
        label="SWE-bench predictions",
    )
    try:
        values = tuple(
            SweBenchPrediction.model_validate_json(line, strict=True)
            for line in body.splitlines()
            if line
        )
    except ValueError:
        raise KernelError(
            "swebench_predictions_invalid", "SWE-bench predictions 缺失、损坏或不符合合同"
        ) from None
    return validate_predictions(plan, values)


def collect_workspace_patch(
    workspace: Path,
    git_executable: Path,
    base_commit: str,
) -> str:
    """用临时 index 收集 tracked、untracked 与删除项，不修改真实 Git index。"""

    root = _resolved_directory(workspace)
    git = _resolved_executable(git_executable)
    if _run_git(git, root, ("rev-parse", "HEAD")).strip() != base_commit.encode("ascii"):
        raise KernelError(
            "swebench_workspace_revision_mismatch",
            "SWE-bench 工作区 HEAD 与预注册 base_commit 不一致",
        )
    with tempfile.TemporaryDirectory(prefix="harnessix-swebench-index-") as directory:
        index = Path(directory) / "index"
        environment = {**_GIT_ENVIRONMENT, "GIT_INDEX_FILE": str(index)}
        _run_git(git, root, ("read-tree", base_commit), environment=environment)
        _run_git(git, root, ("add", "--all", "--", "."), environment=environment)
        body = _run_git(
            git,
            root,
            (
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                base_commit,
                "--",
            ),
            environment=environment,
            max_bytes=_MAX_PATCH_BYTES,
        )
    try:
        patch = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise KernelError("swebench_patch_invalid", "SWE-bench patch 不是 UTF-8") from None
    try:
        SweBenchPrediction(
            instance_id="owner__repo-1",
            model_name_or_path="validation",
            model_patch=patch,
        )
    except ValueError:
        raise KernelError("swebench_patch_invalid", "SWE-bench 工作区 Git diff 无效") from None
    return patch


def _run_git(
    git: Path,
    workspace: Path,
    arguments: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
    max_bytes: int = 4096,
) -> bytes:
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                (
                    str(git),
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *arguments,
                ),
                cwd=workspace,
                env=environment or _GIT_ENVIRONMENT,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
            size = output.tell()
            if result.returncode != 0 or size > max_bytes:
                raise KernelError("swebench_git_failed", "SWE-bench 固定 Git 命令失败")
            output.seek(0)
            return output.read(max_bytes + 1)
    except (OSError, subprocess.SubprocessError):
        raise KernelError("swebench_git_failed", "SWE-bench 固定 Git 命令失败") from None


def _resolved_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise OSError
        return resolved
    except (OSError, RuntimeError):
        raise KernelError("swebench_workspace_invalid", "SWE-bench 工作区无效") from None


def _resolved_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            raise OSError
        return resolved
    except (OSError, RuntimeError):
        raise KernelError("swebench_git_invalid", "SWE-bench Git 绑定无效") from None
