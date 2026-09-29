"""以预冻结单平台Profile运行完整产品重启候选并独立复验。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.run_restart_soak_release import _revision
from scripts.soak_attempt import SoakAttemptStartV2, read_attempt
from scripts.soak_environment import read_environment
from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakManifestV5, SoakProfileReference
from scripts.soak_restart import run_product_restart
from scripts.soak_threshold import (
    SoakThresholdProfile,
    read_profile,
    read_report,
    verify_and_publish,
    verify_profile_baseline,
)

_ARCHIVE = (
    Path(__file__).resolve().parents[1]
    / "docs/validation/soak-restart-three-platform-2026-09-23-v1"
)
_AUTHENTICATED_ARCHIVE = (
    Path(__file__).resolve().parents[1]
    / "docs/validation/authenticated-restart-three-platform-2026-09-29-v1"
)
_AUTHENTICATED_BASELINE_REVISION = "7cbe358ff0f22ea2bc0813478bb1c13a2b1e7c46"
_BASELINE_SETS = ("legacy-unprotected-v1", "authenticated-v1")


def _archive_for_set(baseline_set: str) -> Path:
    """认证负载只能选择专属原件；缺失时不得回退历史基线。"""

    if baseline_set == "legacy-unprotected-v1":
        return _ARCHIVE
    if baseline_set == "authenticated-v1":
        return _AUTHENTICATED_ARCHIVE
    raise KernelError("soak_profile_invalid", "产品重启基线集合无效")


def _only_profile(platform: str, *, archive: Path | None = None) -> Path:
    """本平台必须恰有一个已冻结Profile，不根据文件时间选择。"""

    root = (archive if archive is not None else _ARCHIVE) / "profiles" / platform
    try:
        entries = tuple(root.iterdir())
    except OSError:
        raise KernelError("soak_profile_invalid", "产品重启平台Profile目录不可读") from None
    if (
        len(entries) != 1
        or re.fullmatch(r"[0-9a-f]{32}", entries[0].name) is None
        or not entries[0].is_dir()
        or entries[0].is_symlink()
    ):
        raise KernelError("soak_profile_invalid", "产品重启平台Profile目录不唯一或无效")
    return entries[0]


def _check_authenticated_policy(profile: SoakThresholdProfile, baseline: SoakManifestV5) -> None:
    """认证基线绑定实际采集源码，并逐项保持历史负载和余量规则。"""

    original, _ = read_profile(_only_profile(profile.platform))
    if (
        baseline.code_revision != _AUTHENTICATED_BASELINE_REVISION
        or profile.load != original.load
        or profile.sample_counts != original.sample_counts
        or profile.expected_fault_counts != original.expected_fault_counts
        or profile.provider_script_version != original.provider_script_version
        or (profile.python_min, profile.python_max) != (original.python_min, original.python_max)
        or any(
            limit.margin_basis_points != original.metric_limits[name].margin_basis_points
            for name, limit in profile.metric_limits.items()
        )
        or any(
            limit.margin_basis_points != original.growth_limits[name].margin_basis_points
            for name, limit in profile.growth_limits.items()
        )
    ):
        raise KernelError("soak_profile_baseline_invalid", "认证重启基线来源或原工程规则不匹配")


async def run_candidate(
    evidence_root: Path,
    report_root: Path,
    *,
    baseline_set: str = "authenticated-v1",
) -> dict[str, str]:
    """先核对只读基线，再执行固定规模候选并重读不可覆盖报告。"""

    archive = _archive_for_set(baseline_set)
    environment = read_environment()
    platform = environment.platform
    profile_directory = _only_profile(platform, archive=archive)
    profile, profile_sha = read_profile(profile_directory)
    baseline_directory = archive / "raw" / platform / profile.baseline_run_id
    # 负载前也核对数学阈值与完整Attempt，不能只在候选完成后发现基线无效。
    baseline, baseline_sha = verify_profile_baseline(profile, baseline_directory)
    _, baseline_final = read_attempt(baseline_directory.parent / "attempts" / baseline.run_id)
    if (
        not isinstance(baseline, SoakManifestV5)
        or profile.platform != platform
        or profile.scenario_id != "restart"
        or profile.baseline_manifest_sha256 != baseline_sha
        or baseline.status != "baseline"
        or baseline_final is None
        or baseline_final.outcome != "committed"
        or baseline_final.manifest_sha256 != baseline_sha
        or baseline.load.thread_count != 500
        or baseline.load.warmup_count != 1
        or baseline.load.fault_matrix_version != "product-restart-v1"
        or baseline.sample_counts != {"product_startup": 3, "rss_peak": 1}
        or baseline.fault_counts.eof != 1
    ):
        raise KernelError("soak_profile_baseline_invalid", "产品重启冻结基线不完整")
    if baseline_set == "authenticated-v1":
        _check_authenticated_policy(profile, baseline)
    if environment.hardware_class != profile.hardware_class or not tuple(
        map(int, profile.python_min.split("."))
    ) <= tuple(map(int, environment.python_version.split("."))) <= tuple(
        map(int, profile.python_max.split("."))
    ):
        # 浮动托管机即使更快也不能借用其他资源档位的Profile。
        raise KernelError("soak_environment_mismatch", "产品重启执行环境不匹配冻结Profile")

    revision = _revision()
    reference = SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha)
    candidate_directory, candidate = await run_product_restart(
        evidence_root,
        code_revision=revision,
        thread_count=500,
        warmup_count=1,
        measured_restarts=3,
        timeout_seconds=120,
        threshold_profile_ref=reference,
    )
    restored, candidate_sha = read_published_run(candidate_directory)
    started, final = read_attempt(evidence_root / "attempts" / candidate.run_id)
    if (
        restored != candidate
        or candidate.code_revision != revision
        or candidate.status != "unverified"
        or candidate.threshold_profile_ref != reference
        or not isinstance(started, SoakAttemptStartV2)
        or started.threshold_profile_ref != reference
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != candidate_sha
    ):
        raise KernelError("soak_run_invalid", "产品重启候选Run与预绑定Attempt不一致")
    report_directory, report = verify_and_publish(
        profile_directory, baseline_directory, candidate_directory, report_root
    )
    if read_report(report_directory) != report:
        raise KernelError("soak_report_invalid", "产品重启复验报告重读不一致")
    return {
        "baseline_set": baseline_set,
        "scenario_id": "restart",
        "platform": platform,
        "code_revision": revision,
        "profile_id": profile.profile_id,
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "status": report.status,
        "reason": report.reason,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="以冻结阈值运行完整产品重启独立复验")
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--report-root", required=True, type=Path)
    parser.add_argument("--baseline-set", choices=_BASELINE_SETS, default="authenticated-v1")
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(
            run_candidate(
                arguments.evidence_root,
                arguments.report_root,
                baseline_set=arguments.baseline_set,
            )
        )
    except KernelError as error:
        print(f"产品重启复验失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 不输出异常、私有路径或子进程stderr。
        print("产品重启复验失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
