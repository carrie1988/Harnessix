"""在干净Revision运行Action恢复固定故障矩阵正式负载并复核低敏原件。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.run_restart_soak_release import _revision
from scripts.soak_action_recovery import FAULT_MATRIX_VERSION, run_action_recovery
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run


async def run_release(evidence_root: Path) -> dict[str, str]:
    """固定2轮预热与20轮正式故障矩阵，不接受降配。"""

    revision = _revision()
    run_directory, manifest = await run_action_recovery(
        evidence_root,
        code_revision=revision,
        cycle_count=20,
        warmup_count=2,
        scan_timeout_seconds=30,
    )
    restored, manifest_sha256 = read_published_run(run_directory)
    _, final = read_attempt(evidence_root / "attempts" / manifest.run_id)
    if (
        restored != manifest
        or manifest.code_revision != revision
        or manifest.status != "baseline"
        or manifest.load.fault_matrix_version != FAULT_MATRIX_VERSION
        or manifest.load.warmup_count != 2
        or manifest.sample_counts != {"recovery_scan": 20, "rss_peak": 1}
        or manifest.fault_counts.unknown_effect != 44
        or manifest.fault_counts.duplicate_effect != 0
        or manifest.fault_counts.orphan != 0
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != manifest_sha256
    ):
        raise KernelError("soak_run_invalid", "正式Action恢复Run与Attempt复核不一致")
    return {
        "scenario_id": "action_recovery",
        "platform": manifest.platform,
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": manifest_sha256,
        "status": manifest.status,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定的Action恢复正式基线")
    parser.add_argument("--evidence-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_release(arguments.evidence_root))
    except KernelError as error:
        print(f"Action恢复Soak失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 发布日志不输出路径、Plan身份、异常正文或子进程stderr。
        print("Action恢复Soak失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
