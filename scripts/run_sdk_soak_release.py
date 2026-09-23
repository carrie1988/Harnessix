"""在干净Revision运行固定SDK容量负载并复核可上传的低敏证据。"""

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
from scripts.soak_sdk_capacity import run_sdk_capacity

_REPOSITORY = Path(__file__).resolve().parents[1]


def _revision() -> str:
    """只读获取源码提交；工作树清洁性由正式Runner在写Attempt前核验。"""

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
        raise KernelError("soak_revision_invalid", "正式Soak无法读取源码Revision") from None


async def run_release(evidence_root: Path) -> dict[str, str]:
    """固定64容量、1轮预热、3轮正式负载与20次往返，不接受降配。"""

    revision = _revision()
    run_directory, manifest = await run_sdk_capacity(
        evidence_root,
        code_revision=revision,
        measured_rounds=3,
        warmup_count=1,
        roundtrip_count=20,
        pending_limit=64,
        timeout_seconds=30,
    )
    restored, manifest_sha256 = read_published_run(run_directory)
    _, final = read_attempt(evidence_root / "attempts" / manifest.run_id)
    if (
        restored != manifest
        or manifest.code_revision != revision
        or manifest.status != "baseline"
        or final is None
        or final.outcome != "committed"
        or final.manifest_sha256 != manifest_sha256
    ):
        raise KernelError("soak_run_invalid", "正式SDK Run与Attempt复核不一致")
    return {
        "scenario_id": "sdk_capacity",
        "platform": manifest.platform,
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": manifest_sha256,
        "status": manifest.status,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定的SDK容量正式基线")
    parser.add_argument("--evidence-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_release(arguments.evidence_root))
    except KernelError as error:
        print(f"SDK Soak失败：{error.code}", file=sys.stderr)
        return 1
    except Exception:
        # 发布日志不输出异常正文、临时Workspace路径或子进程stderr。
        print("SDK Soak失败：internal_error", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
