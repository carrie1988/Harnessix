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
from scripts.soak_threshold import read_profile, read_report, verify_and_publish

_ARCHIVE = (
    Path(__file__).resolve().parents[1]
    / "docs/validation/soak-restart-three-platform-2026-09-23-v1"
)


def _only_profile(platform: str) -> Path:
    """本平台必须恰有一个已冻结Profile，不根据文件时间选择。"""

    root = _ARCHIVE / "profiles" / platform
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


async def run_candidate(evidence_root: Path, report_root: Path) -> dict[str, str]:
    """先核对只读基线，再执行固定规模候选并重读不可覆盖报告。"""

    platform = read_environment().platform
    profile_directory = _only_profile(platform)
    profile, profile_sha = read_profile(profile_directory)
    baseline_directory = _ARCHIVE / "raw" / platform / profile.baseline_run_id
    baseline, baseline_sha = read_published_run(baseline_directory)
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
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_candidate(arguments.evidence_root, arguments.report_root))
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
