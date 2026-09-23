"""在干净Revision运行完整产品重启正式负载并复核低敏原件。"""

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
from scripts.soak_manifest import SoakManifestV5
from scripts.soak_restart import run_product_restart

_REPOSITORY = Path(__file__).resolve().parents[1]


def _revision() -> str:
    """源码清洁性由正式Runner在创建Attempt前校验。"""

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
        raise KernelError("soak_revision_invalid", "正式重启Soak无法读取Revision") from None


async def run_release(evidence_root: Path) -> dict[str, str]:
    """固定500 Thread、一次预热、一次硬退出和三次正式新进程启动。"""

    revision = _revision()
    run_directory, manifest = await run_product_restart(
        evidence_root,
        code_revision=revision,
        thread_count=500,
        warmup_count=1,
        measured_restarts=3,
        timeout_seconds=120,
    )
    restored, manifest_sha256 = read_published_run(run_directory)
    _, final = read_attempt(evidence_root / "attempts" / manifest.run_id)
    if (
        not isinstance(restored, SoakManifestV5)
        or restored != manifest
        or manifest.code_revision != revision
        or manifest.status != "baseline"
        or manifest.load.thread_count != 500
        or manifest.sample_counts != {"product_startup": 3, "rss_peak": 1}
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != manifest_sha256
    ):
        raise KernelError("soak_run_invalid", "正式产品重启Run与Attempt复核不一致")
    return {
        "scenario_id": "restart",
        "platform": manifest.platform,
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": manifest_sha256,
        "status": manifest.status,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定的完整产品重启正式基线")
    parser.add_argument("--evidence-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_release(arguments.evidence_root))
    except KernelError as error:
        print(f"产品重启Soak失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 发布日志不输出路径、Thread身份、异常正文或子进程stderr。
        print("产品重启Soak失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
