"""在干净Revision运行长会话Context/Compaction正式负载并复核低敏原件。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.run_restart_soak_release import _revision
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_long_session import run_long_session_context


async def run_release(evidence_root: Path) -> dict[str, str]:
    """固定1000 Turn、5次预热与真实Context/Compaction，不接受降配。"""

    revision = _revision()
    run_directory, manifest = await run_long_session_context(
        evidence_root,
        code_revision=revision,
        turn_count=1000,
        warmup_count=5,
        seed=0,
        turn_timeout_seconds=30,
    )
    restored, manifest_sha256 = read_published_run(run_directory)
    _, final = read_attempt(evidence_root / "attempts" / manifest.run_id)
    if (
        restored != manifest
        or manifest.code_revision != revision
        or manifest.status != "baseline"
        or manifest.spec_version != "harnessix.soak-manifest/v2"
        or manifest.load.turn_count != 1000
        or manifest.load.warmup_count != 5
        or manifest.sample_counts != {"turn_local": 1000, "rss_peak": 1}
        or manifest.summary_request_count < 1
        or any(manifest.fault_counts.model_dump().values())
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != manifest_sha256
    ):
        raise KernelError("soak_run_invalid", "正式长会话Run与Attempt复核不一致")
    return {
        "scenario_id": "long_session",
        "platform": manifest.platform,
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": manifest_sha256,
        "status": manifest.status,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定的长会话Context正式基线")
    parser.add_argument("--evidence-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_release(arguments.evidence_root))
    except KernelError as error:
        print(f"长会话Soak失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 发布日志不输出路径、Thread身份、异常正文或模型正文。
        print("长会话Soak失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
