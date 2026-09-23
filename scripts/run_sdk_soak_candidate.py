"""以仓库内预冻结Profile运行SDK容量候选负载并发布独立复验报告。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.run_sdk_soak_release import _revision
from scripts.soak_attempt import SoakAttemptStartV2, read_attempt
from scripts.soak_environment import read_environment
from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakProfileReference
from scripts.soak_sdk_capacity import run_sdk_capacity
from scripts.soak_threshold import read_profile, read_report, verify_and_publish

_ARCHIVE = (
    Path(__file__).resolve().parents[1] / "docs/validation/soak-sdk-three-platform-2026-09-23-v1"
)


def _only_profile(platform: str) -> Path:
    """每个平台恰有一个冻结Profile；拒绝歧义，不选择“最新”目录。"""

    root = _ARCHIVE / "profiles" / platform
    try:
        entries = tuple(root.iterdir())
    except OSError:
        raise KernelError("soak_profile_invalid", "SDK平台Profile目录不可读") from None
    if (
        len(entries) != 1
        or re.fullmatch(r"[0-9a-f]{32}", entries[0].name) is None
        or not entries[0].is_dir()
        or entries[0].is_symlink()
    ):
        raise KernelError("soak_profile_invalid", "SDK平台Profile目录不唯一或无效")
    return entries[0]


async def run_candidate(evidence_root: Path, report_root: Path) -> dict[str, str]:
    """先核对预冻结基线，再执行固定规模负载并只按独立报告判定。"""

    platform = read_environment().platform
    profile_directory = _only_profile(platform)
    profile, profile_sha = read_profile(profile_directory)
    baseline_directory = _ARCHIVE / "raw" / platform / profile.baseline_run_id
    baseline, baseline_sha = read_published_run(baseline_directory)
    _, baseline_final = read_attempt(baseline_directory.parent / "attempts" / baseline.run_id)
    if (
        profile.platform != platform
        or profile.scenario_id != "sdk_capacity"
        or profile.baseline_manifest_sha256 != baseline_sha
        or baseline.status != "baseline"
        or baseline_final is None
        or baseline_final.outcome != "committed"
        or baseline_final.manifest_sha256 != baseline_sha
        or baseline.load.pending_limit != 64
        or baseline.load.warmup_count != 1
        or baseline.sample_counts != {"sdk_roundtrip": 20, "rss_peak": 1}
    ):
        raise KernelError("soak_profile_baseline_invalid", "SDK冻结基线不完整")

    revision = _revision()
    reference = SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha)
    candidate_directory, candidate = await run_sdk_capacity(
        evidence_root,
        code_revision=revision,
        measured_rounds=3,
        warmup_count=1,
        roundtrip_count=20,
        pending_limit=64,
        timeout_seconds=30,
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
        raise KernelError("soak_run_invalid", "SDK候选Run与预绑定Attempt不一致")
    report_directory, report = verify_and_publish(
        profile_directory, baseline_directory, candidate_directory, report_root
    )
    if read_report(report_directory) != report:
        raise KernelError("soak_report_invalid", "SDK复验报告重读不一致")
    return {
        "scenario_id": "sdk_capacity",
        "platform": platform,
        "code_revision": revision,
        "profile_id": profile.profile_id,
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "status": report.status,
        "reason": report.reason,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="以冻结阈值运行SDK容量独立复验")
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--report-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_candidate(arguments.evidence_root, arguments.report_root))
    except KernelError as error:
        print(f"SDK复验失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 不输出异常、临时路径、用户Workspace或子进程stderr。
        print("SDK复验失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
