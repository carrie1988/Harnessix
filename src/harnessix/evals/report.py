"""Eval 报告的有界原子读写。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan, CodingEvalCampaignReport
from harnessix.evals.contracts import CodingEvalReport

MAX_EVAL_REPORT_BYTES = 1024 * 1024
MAX_EVAL_CAMPAIGN_PLAN_BYTES = 256 * 1024
MAX_EVAL_CAMPAIGN_REPORT_BYTES = 1024 * 1024


def eval_report_sha256(report: CodingEvalReport) -> str:
    body = (report.model_dump_json(indent=2) + "\n").encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _write_report(
    path: Path,
    report: ContractModel,
    max_bytes: int,
    *,
    too_large_code: str,
    path_denied_code: str,
    write_failed_code: str,
    label: str,
) -> None:
    body = (report.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(body) > max_bytes:
        raise KernelError(too_large_code, f"{label}超过大小上限")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        parent = path.parent.resolve(strict=True)
        target = parent / path.name
        if target.is_symlink():
            raise KernelError(path_denied_code, f"{label}目标不能是符号链接")
        temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except KernelError:
        raise
    except OSError:
        raise KernelError(write_failed_code, f"{label}写入失败") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_report[T: ContractModel](
    path: Path,
    model: type[T],
    max_bytes: int,
    *,
    invalid_code: str,
    label: str,
    require_private_mode: bool = False,
) -> T:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > max_bytes
            or (require_private_mode and info.st_mode & 0o777 != 0o600)
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > max_bytes:
            raise OSError
        return model.model_validate_json(body, strict=True)
    except (OSError, ValueError, ValidationError):
        raise KernelError(invalid_code, f"{label}缺失、损坏或超过上限") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_eval_report(path: Path, report: CodingEvalReport) -> None:
    """在目标目录内 fsync 临时文件后原子替换，不跟随既有符号链接。"""

    _write_report(
        path,
        report,
        MAX_EVAL_REPORT_BYTES,
        too_large_code="eval_report_too_large",
        path_denied_code="eval_report_path_denied",
        write_failed_code="eval_report_write_failed",
        label="Eval 报告",
    )


def read_eval_report(path: Path) -> CodingEvalReport:
    return _read_report(
        path,
        CodingEvalReport,
        MAX_EVAL_REPORT_BYTES,
        invalid_code="eval_report_invalid",
        label="Eval 报告",
    )


def write_eval_campaign_plan(path: Path, plan: CodingEvalCampaignPlan) -> None:
    """在首个请求前原子固定Campaign运行ID、环境和价格上下文。"""

    try:
        plan = CodingEvalCampaignPlan.model_validate_json(plan.model_dump_json(), strict=True)
    except ValueError:
        raise KernelError("eval_campaign_plan_invalid", "Campaign计划无效") from None
    _write_report(
        path,
        plan,
        MAX_EVAL_CAMPAIGN_PLAN_BYTES,
        too_large_code="eval_campaign_plan_too_large",
        path_denied_code="eval_campaign_plan_path_denied",
        write_failed_code="eval_campaign_plan_write_failed",
        label="Campaign计划",
    )


def read_eval_campaign_plan(path: Path) -> CodingEvalCampaignPlan:
    return _read_report(
        path,
        CodingEvalCampaignPlan,
        MAX_EVAL_CAMPAIGN_PLAN_BYTES,
        invalid_code="eval_campaign_plan_invalid",
        label="Campaign计划",
        require_private_mode=True,
    )


def write_eval_campaign_report(path: Path, report: CodingEvalCampaignReport) -> None:
    """原子发布不包含模型正文、Diff、凭据或宿主绝对路径的Campaign报告。"""

    try:
        report = CodingEvalCampaignReport.model_validate_json(report.model_dump_json(), strict=True)
    except ValueError:
        raise KernelError("eval_campaign_report_invalid", "Campaign报告无效") from None
    _write_report(
        path,
        report,
        MAX_EVAL_CAMPAIGN_REPORT_BYTES,
        too_large_code="eval_campaign_report_too_large",
        path_denied_code="eval_campaign_report_path_denied",
        write_failed_code="eval_campaign_report_write_failed",
        label="Campaign报告",
    )


def read_eval_campaign_report(path: Path) -> CodingEvalCampaignReport:
    return _read_report(
        path,
        CodingEvalCampaignReport,
        MAX_EVAL_CAMPAIGN_REPORT_BYTES,
        invalid_code="eval_campaign_report_invalid",
        label="Campaign报告",
        require_private_mode=True,
    )
