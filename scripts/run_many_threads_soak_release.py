"""在干净Revision运行固定多Thread正式负载并复核Run与Attempt。"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_many_threads import run_many_threads

_REPOSITORY = Path(__file__).resolve().parents[1]


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定的多Thread正式基线")
    parser.add_argument("--evidence-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_release(arguments.evidence_root))
    except KernelError as error:
        print(f"多Thread Soak失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 发布日志不输出路径、Thread身份或异常正文。
        print("多Thread Soak失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
