"""使用预冻结单平台Profile运行多Thread第二独立候选并复验。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.run_many_threads_soak_release import _revision
from scripts.soak_attempt import SoakAttemptStartV2, read_attempt
from scripts.soak_environment import read_environment
from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakManifestV6, SoakProfileReference
from scripts.soak_many_threads import run_many_threads
from scripts.soak_threshold import (
    read_profile,
    read_report,
    verify_and_publish,
    verify_profile_baseline,
)
from scripts.soak_worker_guard import guarded_worker

_REPOSITORY = Path(__file__).resolve().parents[1]
_ARCHIVE = _REPOSITORY / "docs/validation/soak-many-threads-three-platform-2026-09-23-v2"
_WORKER_TIMEOUT_SECONDS = 20 * 60
_SUMMARY_FIELDS = frozenset(
    {
        "scenario_id",
        "platform",
        "code_revision",
        "profile_id",
        "baseline_run_id",
        "candidate_run_id",
        "status",
        "reason",
    }
)


def _only_profile(platform: str) -> Path:
    """同平台必须恰好有一个封印Profile，不按文件时间猜选。"""

    root = _ARCHIVE / "profiles" / platform
    try:
        entries = tuple(root.iterdir())
    except OSError:
        raise KernelError("soak_profile_invalid", "多Thread平台Profile目录不可读") from None
    if (
        len(entries) != 1
        or re.fullmatch(r"[0-9a-f]{32}", entries[0].name) is None
        or not entries[0].is_dir()
        or entries[0].is_symlink()
    ):
        raise KernelError("soak_profile_invalid", "多Thread平台Profile目录不唯一或无效")
    return entries[0]


async def run_candidate(evidence_root: Path, report_root: Path) -> dict[str, str]:
    """先完整核对冻结基线，再启动固定500 Thread候选并发布独立报告。"""

    platform = read_environment().platform
    profile_directory = _only_profile(platform)
    profile, profile_sha = read_profile(profile_directory)
    baseline_directory = _ARCHIVE / "raw" / platform / profile.baseline_run_id
    baseline, baseline_sha = verify_profile_baseline(profile, baseline_directory)
    if (
        not isinstance(baseline, SoakManifestV6)
        or profile.platform != platform
        or profile.scenario_id != "many_threads"
        or profile.baseline_manifest_sha256 != baseline_sha
        or baseline.status != "baseline"
        or baseline.load.thread_count != 500
        or baseline.load.warmup_count != 11
        or baseline.sample_counts
        != {"app_service_startup": 3, "thread_list_page": 30, "rss_peak": 1}
        or baseline.provider.request_count != 0
    ):
        raise KernelError("soak_profile_baseline_invalid", "多Thread冻结基线不完整")

    revision = _revision()
    reference = SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha)
    candidate_directory, candidate = await run_many_threads(
        evidence_root,
        code_revision=revision,
        thread_count=500,
        list_limit=50,
        restart_count=3,
        startup_timeout_seconds=120,
        page_timeout_seconds=120,
        threshold_profile_ref=reference,
    )
    restored, candidate_sha = read_published_run(candidate_directory)
    started, final = read_attempt(evidence_root / "attempts" / candidate.run_id)
    if (
        restored != candidate
        or not isinstance(candidate, SoakManifestV6)
        or candidate.code_revision != revision
        or candidate.status != "unverified"
        or candidate.threshold_profile_ref != reference
        or not isinstance(started, SoakAttemptStartV2)
        or started.threshold_profile_ref != reference
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != candidate_sha
    ):
        raise KernelError("soak_run_invalid", "多Thread候选Run与预绑定Attempt不一致")
    report_directory, report = verify_and_publish(
        profile_directory, baseline_directory, candidate_directory, report_root
    )
    if read_report(report_directory) != report:
        raise KernelError("soak_report_invalid", "多Thread复验报告重读不一致")
    return {
        "scenario_id": "many_threads",
        "platform": platform,
        "code_revision": revision,
        "profile_id": profile.profile_id,
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "status": report.status,
        "reason": report.reason,
    }


def _worker_main(evidence_root: Path, report_root: Path) -> int:
    """仅在受进程监护的Worker中运行真实负载。"""

    try:
        result = asyncio.run(run_candidate(evidence_root, report_root))
    except KernelError as error:
        print(f"多Thread复验失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        print("多Thread复验失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _validated_summary(body: str | None) -> dict[str, str] | None:
    """父进程只接受字段和格式均固定的低敏报告摘要。"""

    try:
        if body is None or len(body.encode("utf-8")) > 4096:
            return None
        result = json.loads(body)
    except (TypeError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(result, dict)
        or set(result) != _SUMMARY_FIELDS
        or any(not isinstance(result[field], str) for field in _SUMMARY_FIELDS)
        or result["scenario_id"] != "many_threads"
        or result["platform"] not in {"linux", "macos", "windows"}
        or result["status"] not in {"PASS", "FAIL", "unverified"}
        or re.fullmatch(r"[a-z][a-z0-9_]*", result["reason"]) is None
        or (result["status"] == "PASS") != (result["reason"] == "within_limits")
        or (result["status"] == "FAIL") != (result["reason"] == "limit_exceeded")
        or any(
            re.fullmatch(pattern, result[field]) is None
            for field, pattern in (
                ("code_revision", r"[0-9a-f]{40}"),
                ("profile_id", r"[0-9a-f]{32}"),
                ("baseline_run_id", r"[0-9a-f]{32}"),
                ("candidate_run_id", r"[0-9a-f]{32}"),
            )
        )
    ):
        return None
    return result


def _parent_main(evidence_root: Path, report_root: Path) -> int:
    """以硬期限隔离可能无限等待SQLite排空的候选Runner。"""

    output, error_code = guarded_worker(
        "scripts.run_many_threads_soak_candidate",
        arguments=("--evidence-root", str(evidence_root), "--report-root", str(report_root)),
        repository=_REPOSITORY,
        timeout_seconds=_WORKER_TIMEOUT_SECONDS,
        error_prefix="多Thread复验失败",
    )
    if error_code is not None:
        print(f"多Thread复验失败：{error_code}", file=sys.stderr)
        return 1
    summary = _validated_summary(output)
    if summary is None:
        print("多Thread复验失败：soak_worker_invalid", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["status"] == "PASS" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行多Thread冻结阈值的第二独立候选")
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--report-root", required=True, type=Path)
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.internal_worker:
        return _worker_main(arguments.evidence_root, arguments.report_root)
    return _parent_main(arguments.evidence_root, arguments.report_root)


if __name__ == "__main__":
    raise SystemExit(main())
