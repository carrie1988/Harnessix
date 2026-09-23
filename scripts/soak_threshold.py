"""独立Soak阈值复验：只读取已提交证据，不修改业务状态。"""

from __future__ import annotations

import re
import stat
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import Field, StrictInt, model_validator

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import (
    _ensure_private_root,
    _read_file,
    _sync_directory,
    _write_file,
    read_published_run,
)
from scripts.soak_manifest import MeasurementBoundary, SoakFaultCounts, SoakLoad, SoakManifest
from scripts.soak_samples import SCENARIO_METRICS, ScenarioId

PROFILE_FILENAME = "profile.json"
REPORT_FILENAME = "report.json"
SEAL_FILENAME = "SEALED.json"
MAX_PROFILE_BYTES = 16 * 1024
MAX_REPORT_BYTES = 4 * 1024
MAX_SEAL_BYTES = 256
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _version(value: str) -> tuple[int, int, int]:
    if _VERSION.fullmatch(value) is None:
        raise ValueError("Python版本格式无效")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _growth(before: int, after: int) -> int:
    return max(0, after - before)


class SoakMetricLimit(ContractModel):
    """一项指标三个最近秩分位数的显式上限及工程余量。"""

    unit: Literal["ns", "bytes"]
    direction: Literal["upper"]
    margin_basis_points: StrictInt = Field(ge=0, le=10000)
    p50_upper: StrictInt = Field(ge=0)
    p95_upper: StrictInt = Field(ge=0)
    p99_upper: StrictInt = Field(ge=0)


class SoakGrowthLimit(ContractModel):
    """持久文件正向增长的显式字节上限。"""

    unit: Literal["bytes"]
    direction: Literal["upper"]
    margin_basis_points: StrictInt = Field(ge=0, le=10000)
    upper_bytes: StrictInt = Field(ge=0)


class SoakThresholdProfile(ContractModel):
    """冻结后不可覆盖的单平台阈值；缺字段不能成为PASS依据。"""

    spec_version: Literal["harnessix.soak-threshold/v1"]
    profile_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["frozen"]
    scenario_id: ScenarioId
    scenario_version: str = Field(pattern=r"^harnessix\.soak-scenario/v[1-9][0-9]*$")
    measurement_boundary: MeasurementBoundary
    platform: Literal["linux", "macos", "windows"]
    hardware_class: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    python_min: str
    python_max: str
    baseline_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    baseline_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: StrictInt = Field(ge=0)
    load: SoakLoad
    provider_script_version: str = Field(
        pattern=r"^(harnessix\.soak-provider/v[1-9][0-9]*|harnessix\.product-no-turn/v1)$"
    )
    sample_counts: dict[str, StrictInt]
    quantile_method: Literal["nearest_rank_v1"]
    metric_limits: dict[str, SoakMetricLimit]
    growth_limits: dict[Literal["db", "wal", "artifact"], SoakGrowthLimit]
    expected_fault_counts: SoakFaultCounts
    required_platform_validation: tuple[Literal["linux", "macos", "windows"], ...]
    created_at: datetime

    @model_validator(mode="after")
    def complete_profile(self) -> Self:
        metrics = SCENARIO_METRICS[self.scenario_id]
        if (
            set(self.metric_limits) != metrics
            or set(self.sample_counts) != metrics
            or any(type(count) is not int or count <= 0 for count in self.sample_counts.values())
            or set(self.growth_limits) != {"db", "wal", "artifact"}
            or any(
                limit.unit != ("bytes" if metric == "rss_peak" else "ns")
                for metric, limit in self.metric_limits.items()
            )
            or not self.required_platform_validation
            or len(set(self.required_platform_validation)) != len(self.required_platform_validation)
            or set(self.required_platform_validation) != {"linux", "macos", "windows"}
            or _version(self.python_min) > _version(self.python_max)
            or self.created_at.utcoffset() != UTC.utcoffset(None)
            or (self.scenario_id == "restart")
            != (self.provider_script_version == "harnessix.product-no-turn/v1")
        ):
            raise ValueError("Soak阈值Profile不完整或环境范围无效")
        return self


class SoakVerificationReport(ContractModel):
    """单次独立复验结论；PASS不等于三平台发布通过。"""

    spec_version: Literal["harnessix.soak-verification/v1"]
    report_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    profile_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    baseline_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    candidate_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: Literal["PASS", "FAIL", "unverified"]
    reason: Literal[
        "within_limits",
        "limit_exceeded",
        "evidence_invalid",
        "baseline_mismatch",
        "environment_mismatch",
        "load_mismatch",
        "fault_mismatch",
        "status_unverified",
        "profile_mismatch",
    ]
    violations: tuple[str, ...]
    verified_at: datetime

    @model_validator(mode="after")
    def consistent_conclusion(self) -> Self:
        if (
            (self.status == "PASS") != (self.reason == "within_limits")
            or (self.status == "FAIL") != (self.reason == "limit_exceeded")
            or (self.status == "FAIL") != bool(self.violations)
            or (self.status in {"PASS", "FAIL"} and self.candidate_manifest_sha256 is None)
            or self.verified_at.utcoffset() != UTC.utcoffset(None)
        ):
            raise ValueError("Soak复验结论不一致")
        return self


class SoakEvidenceSeal(ContractModel):
    """最后提交标记，绑定Profile或Report的规范原始字节。"""

    spec_version: Literal["harnessix.soak-evidence-seal/v1"]
    kind: Literal["profile", "report"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _canonical(value: ContractModel) -> bytes:
    return (value.model_dump_json() + "\n").encode("utf-8")


def _published_directory(
    root: Path,
    identity: str,
    name: str,
    body: bytes,
    limit: int,
    kind: Literal["profile", "report"],
) -> Path:
    _ensure_private_root(root)
    directory = root / identity
    try:
        directory.mkdir(mode=0o700, exist_ok=False)
        _sync_directory(root)
    except OSError:
        raise KernelError("soak_evidence_exists", "Soak证据目录已存在或不可创建") from None
    _write_file(directory, name, body, limit)
    seal = SoakEvidenceSeal(
        spec_version="harnessix.soak-evidence-seal/v1", kind=kind, sha256=sha256(body).hexdigest()
    )
    _write_file(directory, SEAL_FILENAME, _canonical(seal), MAX_SEAL_BYTES)
    return directory


def _read_sealed(
    directory: Path, name: str, limit: int, kind: Literal["profile", "report"]
) -> bytes:
    try:
        if (
            directory.is_symlink()
            or bool(getattr(directory, "is_junction", lambda: False)())
            or not stat.S_ISDIR(directory.stat(follow_symlinks=False).st_mode)
        ):
            raise OSError
        if {path.name for path in directory.iterdir()} != {name, SEAL_FILENAME}:
            raise ValueError
        body = _read_file(directory, name, limit)
        seal_body = _read_file(directory, SEAL_FILENAME, MAX_SEAL_BYTES)
        seal = SoakEvidenceSeal.model_validate_json(seal_body)
        if (
            seal_body != _canonical(seal)
            or seal.kind != kind
            or seal.sha256 != sha256(body).hexdigest()
        ):
            raise ValueError
    except (OSError, ValueError, KernelError):
        raise KernelError("soak_evidence_invalid", "Soak证据提交标记无效") from None
    return body


def _complete_run(directory: Path) -> tuple[SoakManifest, str]:
    manifest, digest = read_published_run(directory)
    started, final = read_attempt(directory.parent / "attempts" / manifest.run_id)
    if (
        started.run_id != manifest.run_id
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != digest
    ):
        raise KernelError("soak_attempt_incomplete", "Soak Run缺少完整提交Attempt")
    return manifest, digest


def _margin_upper(baseline: int, basis_points: int) -> int:
    return (baseline * (10_000 + basis_points) + 9_999) // 10_000


def _verify_profile_baseline(
    profile: SoakThresholdProfile, baseline: SoakManifest, digest: str
) -> None:
    # 尚未实现的场景不能凭手工构造的Manifest获得性能PASS。
    if (
        profile.scenario_id
        not in {"long_session", "many_threads", "artifact_growth", "sdk_capacity", "restart"}
        or (
            profile.scenario_id == "long_session"
            and baseline.spec_version != "harnessix.soak-manifest/v2"
        )
        or (
            profile.scenario_id == "many_threads"
            and baseline.spec_version != "harnessix.soak-manifest/v6"
        )
        or (
            profile.scenario_id == "artifact_growth"
            and baseline.spec_version != "harnessix.soak-manifest/v3"
        )
        or (
            profile.scenario_id == "sdk_capacity"
            and baseline.spec_version != "harnessix.soak-manifest/v4"
        )
        or (
            profile.scenario_id == "restart"
            and baseline.spec_version != "harnessix.soak-manifest/v5"
        )
    ):
        raise KernelError("soak_profile_baseline_invalid", "场景缺少正式负载证明")
    if (
        baseline.status != "baseline"
        or baseline.run_id != profile.baseline_run_id
        or digest != profile.baseline_manifest_sha256
        or baseline.scenario_id != profile.scenario_id
        or baseline.scenario_version != profile.scenario_version
        or baseline.measurement_boundary != profile.measurement_boundary
        or baseline.platform != profile.platform
        or baseline.hardware_class != profile.hardware_class
        or baseline.seed != profile.seed
        or baseline.load != profile.load
        or baseline.provider.script_version != profile.provider_script_version
        or baseline.sample_counts != profile.sample_counts
        or baseline.quantile_method != profile.quantile_method
        or baseline.fault_counts != profile.expected_fault_counts
        or not _version(profile.python_min)
        <= _version(baseline.python_version)
        <= _version(profile.python_max)
    ):
        raise KernelError("soak_profile_baseline_invalid", "阈值Profile与基线事实不匹配")
    for metric, limit in profile.metric_limits.items():
        observed = baseline.statistics[metric]
        for percentile in ("p50", "p95", "p99"):
            if getattr(limit, f"{percentile}_upper") != _margin_upper(
                getattr(observed, percentile), limit.margin_basis_points
            ):
                raise KernelError("soak_profile_baseline_invalid", "阈值未按冻结余量展开")
    for kind, limit in profile.growth_limits.items():
        watermarks = baseline.file_watermarks
        observed_growth = _growth(
            getattr(watermarks, f"{kind}_before_bytes"),
            getattr(watermarks, f"{kind}_after_bytes"),
        )
        if limit.upper_bytes != _margin_upper(observed_growth, limit.margin_basis_points):
            raise KernelError("soak_profile_baseline_invalid", "容量阈值未按冻结余量展开")


def publish_profile(
    root: Path, profile: SoakThresholdProfile, baseline_directory: Path
) -> tuple[Path, str]:
    """先核验完整基线与数学阈值，再单写冻结Profile。"""

    baseline, digest = _complete_run(baseline_directory)
    _verify_profile_baseline(profile, baseline, digest)
    body = _canonical(profile)
    directory = _published_directory(
        root, profile.profile_id, PROFILE_FILENAME, body, MAX_PROFILE_BYTES, "profile"
    )
    return directory, sha256(body).hexdigest()


def verify_profile_baseline(
    profile: SoakThresholdProfile, baseline_directory: Path
) -> tuple[SoakManifest, str]:
    """候选启动前重读完整基线与冻结阈值，拒绝赛后补证据。"""

    baseline, digest = _complete_run(baseline_directory)
    _verify_profile_baseline(profile, baseline, digest)
    return baseline, digest


def read_profile(directory: Path) -> tuple[SoakThresholdProfile, str]:
    """只接受规范字节与精确文件集。"""

    try:
        body = _read_sealed(directory, PROFILE_FILENAME, MAX_PROFILE_BYTES, "profile")
        profile = SoakThresholdProfile.model_validate_json(body)
        if body != _canonical(profile) or profile.profile_id != directory.name:
            raise ValueError
    except (OSError, ValueError, KernelError):
        raise KernelError("soak_profile_invalid", "Soak阈值Profile文件无效") from None
    return profile, sha256(body).hexdigest()


def verify_and_publish(
    profile_directory: Path,
    baseline_directory: Path,
    candidate_directory: Path,
    report_root: Path,
) -> tuple[Path, SoakVerificationReport]:
    """独立重读两次Run与Attempt，按预冻结上限发布一次不可覆盖结论。"""

    profile, profile_sha = read_profile(profile_directory)
    if re.fullmatch(r"[0-9a-f]{32}", candidate_directory.name) is None:
        raise KernelError("soak_run_invalid", "复验Run目录身份无效")
    status: Literal["PASS", "FAIL", "unverified"] = "unverified"
    reason: str = "evidence_invalid"
    violations: tuple[str, ...] = ()
    candidate_sha: str | None = None
    try:
        baseline, baseline_sha = _complete_run(baseline_directory)
        _verify_profile_baseline(profile, baseline, baseline_sha)
    except KernelError as error:
        reason = (
            "baseline_mismatch"
            if error.code == "soak_profile_baseline_invalid"
            else "evidence_invalid"
        )
    else:
        try:
            candidate, candidate_sha = _complete_run(candidate_directory)
        except KernelError:
            reason = "evidence_invalid"
        else:
            if candidate.run_id == baseline.run_id or (
                candidate.scenario_id != baseline.scenario_id
                or candidate.scenario_version != baseline.scenario_version
                or candidate.measurement_boundary != baseline.measurement_boundary
                or candidate.seed != baseline.seed
                or candidate.provider.mode != baseline.provider.mode
                or candidate.provider.script_version != baseline.provider.script_version
                or candidate.load != baseline.load
                or candidate.sample_counts != baseline.sample_counts
                or candidate.quantile_method != baseline.quantile_method
            ):
                reason = "load_mismatch"
            elif (
                candidate.platform != profile.platform
                or candidate.hardware_class != profile.hardware_class
                or not _version(profile.python_min)
                <= _version(candidate.python_version)
                <= _version(profile.python_max)
            ):
                reason = "environment_mismatch"
            elif (
                candidate.threshold_profile_ref is None
                or candidate.threshold_profile_ref.profile_id != profile.profile_id
                or candidate.threshold_profile_ref.sha256 != profile_sha
            ):
                reason = "profile_mismatch"
            elif candidate.status != "unverified" or not candidate.rss.unit_verified:
                reason = "status_unverified"
            elif candidate.fault_counts != profile.expected_fault_counts:
                reason = "fault_mismatch"
            else:
                exceeded: list[str] = []
                for metric, limit in profile.metric_limits.items():
                    observed = candidate.statistics[metric]
                    for percentile in ("p50", "p95", "p99"):
                        if getattr(observed, percentile) > getattr(limit, f"{percentile}_upper"):
                            exceeded.append(f"{metric}.{percentile}")
                for kind, limit in profile.growth_limits.items():
                    watermarks = candidate.file_watermarks
                    if (
                        _growth(
                            getattr(watermarks, f"{kind}_before_bytes"),
                            getattr(watermarks, f"{kind}_after_bytes"),
                        )
                        > limit.upper_bytes
                    ):
                        exceeded.append(f"{kind}_growth")
                violations = tuple(sorted(exceeded))
                status = "FAIL" if violations else "PASS"
                reason = "limit_exceeded" if violations else "within_limits"

    report = SoakVerificationReport(
        spec_version="harnessix.soak-verification/v1",
        report_id=uuid4().hex,
        profile_id=profile.profile_id,
        profile_sha256=profile_sha,
        baseline_run_id=profile.baseline_run_id,
        baseline_manifest_sha256=profile.baseline_manifest_sha256,
        candidate_run_id=candidate_directory.name,
        candidate_manifest_sha256=candidate_sha,
        status=status,
        reason=reason,
        violations=violations,
        verified_at=datetime.now(UTC),
    )
    directory = _published_directory(
        report_root,
        report.report_id,
        REPORT_FILENAME,
        _canonical(report),
        MAX_REPORT_BYTES,
        "report",
    )
    return directory, report


def read_report(directory: Path) -> SoakVerificationReport:
    """拒绝附加文件、重写字段和非规范报告。"""

    try:
        body = _read_sealed(directory, REPORT_FILENAME, MAX_REPORT_BYTES, "report")
        report = SoakVerificationReport.model_validate_json(body)
        if body != _canonical(report) or report.report_id != directory.name:
            raise ValueError
    except (OSError, ValueError, KernelError):
        raise KernelError("soak_report_invalid", "Soak复验报告文件无效") from None
    return report
