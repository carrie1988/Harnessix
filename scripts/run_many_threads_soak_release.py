"""在干净Revision运行固定多Thread正式负载并复核Run与Attempt。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_many_threads import run_many_threads
from scripts.soak_worker_guard import guarded_worker

_REPOSITORY = Path(__file__).resolve().parents[1]
_WORKER_TIMEOUT_SECONDS = 20 * 60
_SUMMARY_FIELDS = frozenset(
    {"scenario_id", "platform", "code_revision", "run_id", "manifest_sha256", "status"}
)


def _revision() -> str:
    """只读取源码提交；工作树清洁性由正式Runner在写Attempt前核验。"""

    try:
        return subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=_REPOSITORY,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise KernelError("soak_revision_invalid", "多Thread Soak无法读取源码Revision") from None


async def run_release(evidence_root: Path) -> dict[str, str]:
    """固定500 Thread、50条每页、一次预热与三次正式重启。"""

    revision = _revision()
    run_directory, manifest = await run_many_threads(
        evidence_root,
        code_revision=revision,
        thread_count=500,
        list_limit=50,
        restart_count=3,
        startup_timeout_seconds=120,
        page_timeout_seconds=120,
    )
    restored, manifest_sha256 = read_published_run(run_directory)
    _, final = read_attempt(evidence_root / "attempts" / manifest.run_id)
    if (
        restored != manifest
        or manifest.code_revision != revision
        or manifest.scenario_id != "many_threads"
        or manifest.status != "baseline"
        or manifest.load.thread_count != 500
        or manifest.load.warmup_count != 11
        or manifest.sample_counts
        != {"app_service_startup": 3, "thread_list_page": 30, "rss_peak": 1}
        or manifest.provider.request_count != 0
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != manifest_sha256
    ):
        raise KernelError("soak_run_invalid", "正式多Thread Run与Attempt复核不一致")
    return {
        "scenario_id": "many_threads",
        "platform": manifest.platform,
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": manifest_sha256,
        "status": manifest.status,
    }


def _worker_main(evidence_root: Path) -> int:
    """仅在受监护的子进程内运行既有正式Runner。"""

    try:
        result = asyncio.run(run_release(evidence_root))
    except KernelError as error:
        print(f"多Thread Soak失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 发布日志不输出路径、Thread身份或异常正文。
        print("多Thread Soak失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _validated_summary(body: str) -> str | None:
    """父进程仅转发固定的低敏摘要字段。"""

    try:
        if len(body.encode("utf-8")) > 4096:
            return None
        result = json.loads(body)
    except (TypeError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(result, dict)
        or set(result) != _SUMMARY_FIELDS
        or any(not isinstance(result[key], str) for key in _SUMMARY_FIELDS)
        or result["scenario_id"] != "many_threads"
        or result["platform"] not in {"linux", "macos", "windows"}
        or result["status"] != "baseline"
        or any(
            re.fullmatch(pattern, result[key]) is None
            for key, pattern in (
                ("code_revision", r"[0-9a-f]{40}"),
                ("run_id", r"[0-9a-f]{32}"),
                ("manifest_sha256", r"[0-9a-f]{64}"),
            )
        )
    ):
        return None
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def _parent_main(evidence_root: Path) -> int:
    """以进程级硬期限包住可能无限等待SQLite排空的Runner。"""

    output, error_code = guarded_worker(
        "scripts.run_many_threads_soak_release",
        arguments=("--evidence-root", str(evidence_root)),
        repository=_REPOSITORY,
        timeout_seconds=_WORKER_TIMEOUT_SECONDS,
        error_prefix="多Thread Soak失败",
    )
    if error_code is not None:
        print(f"多Thread Soak失败：{error_code}", file=sys.stderr)
        return 1
    summary = _validated_summary(output) if output is not None else None
    if summary is None:
        print("多Thread Soak失败：soak_worker_invalid", file=sys.stderr)
        return 1
    print(summary)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定的多Thread正式基线")
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.internal_worker:
        return _worker_main(arguments.evidence_root)
    return _parent_main(arguments.evidence_root)


if __name__ == "__main__":
    raise SystemExit(main())
