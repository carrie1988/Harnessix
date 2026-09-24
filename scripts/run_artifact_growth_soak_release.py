"""在干净Revision运行Artifact混合增长正式负载并复核低敏原件。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.run_restart_soak_release import _revision
from scripts.soak_artifact_growth import run_artifact_growth
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run


async def run_release(evidence_root: Path) -> dict[str, str]:
    """固定2件预热与300件正式混合大小件，不接受降配。"""

    revision = _revision()
    run_directory, manifest = await run_artifact_growth(
        evidence_root,
        code_revision=revision,
        turn_count=300,
        warmup_count=2,
        seed=0,
    )
    restored, manifest_sha256 = read_published_run(run_directory)
    _, final = read_attempt(evidence_root / "attempts" / manifest.run_id)
    if (
        restored != manifest
        or manifest.code_revision != revision
        or manifest.status != "baseline"
        or manifest.spec_version != "harnessix.soak-manifest/v3"
        or manifest.load.warmup_count != 2
        or manifest.load.artifact_count != 302
        or manifest.sample_counts["artifact_publish"] != 300
        or manifest.sample_counts["artifact_read"] < 300
        or manifest.sample_counts["rss_peak"] != 1
        or any(manifest.fault_counts.model_dump().values())
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != manifest_sha256
    ):
        raise KernelError("soak_run_invalid", "正式Artifact Run与Attempt复核不一致")
    return {
        "scenario_id": "artifact_growth",
        "platform": manifest.platform,
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": manifest_sha256,
        "status": manifest.status,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定的Artifact增长正式基线")
    parser.add_argument("--evidence-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_release(arguments.evidence_root))
    except KernelError as error:
        print(f"Artifact Soak失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 发布日志不输出路径、Thread身份、异常正文或Artifact正文。
        print("Artifact Soak失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
