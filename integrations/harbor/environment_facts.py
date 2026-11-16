"""任务环境的系统信息：智能体实际运行的系统与架构，连同评测主机的系统，写入试次的agent/environment.json。

导入后进入executor_refs，Langfuse据此生成userId“系统@版本 · 系统 · 架构”（2026-11-16用户决定）。
- 运行时：适配器在安装阶段于任务容器内探测一次（不计入智能体耗时）；失败不影响试次，只缺这份记录。
- 补读：在加入运行时探测之前开跑、没有这份记录的试次，用任务镜像在断网的一次性容器里执行同一探测，
  记为source=task_image。用法：python -m integrations.harbor.environment_facts --run-dir <运行目录>
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

# 与evalkit.harbor的ENVIRONMENT_FILE、ENVIRONMENT_SPEC一致（由测试核对）；适配器不导入evalkit。
ENVIRONMENT_FILE = "environment.json"
ENVIRONMENT_SPEC = "evalkit.agent-environment/v1"
# 只读os-release与uname，POSIX sh即可执行；缺失的字段输出空行，固定5行。
PROBE = (
    ". /etc/os-release 2>/dev/null; "
    'printf "%s\\n%s\\n%s\\n" "${NAME:-}" "${VERSION_ID:-}" "${PRETTY_NAME:-}"; '
    "uname -s; uname -m"
)
Source = Literal["runtime", "task_image"]


class _ExecResult(Protocol):
    return_code: int
    stdout: str | None


class _Environment(Protocol):
    async def exec(self, command: str, timeout_sec: int | None = None) -> _ExecResult: ...


def environment_facts(probe_output: str, source: Source) -> dict[str, str] | None:
    """探测输出的最后5行依次为NAME、VERSION_ID、PRETTY_NAME、内核名、架构；系统或架构缺失时返回None。"""
    lines = [line.strip() for line in probe_output.splitlines()[-5:]]
    if len(lines) != 5:
        return None
    name, version, pretty, kernel, arch = lines
    system = " ".join(part for part in (name, version) if part) or pretty or kernel
    if not system or not arch:
        return None
    return {
        "spec_version": ENVIRONMENT_SPEC,
        "source": source,
        "os": system,
        "os_pretty": pretty or system,
        "arch": arch,
        **_host(),
    }


def _host() -> dict[str, str]:
    """评测主机（Docker宿主）的系统；容器共用它的内核。"""
    try:
        release = platform.freedesktop_os_release()
        host = release.get("PRETTY_NAME") or release.get("NAME") or platform.system()
    except OSError:
        host = platform.system()
    return {"host_os": host, "host_kernel": platform.release(), "host_arch": platform.machine()}


def _write(agent_dir: Path, facts: dict[str, str]) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ENVIRONMENT_FILE).write_text(json.dumps(facts, ensure_ascii=False), "utf-8")


async def record_environment(environment: _Environment, agent_dir: Path) -> None:
    """安装阶段调用一次；探测失败、超时或输出不完整时不写记录，也不改变试次结果。"""
    try:
        result = await environment.exec(command=PROBE, timeout_sec=30)
    except Exception:  # noqa: BLE001 - 辅助证据，任何失败都不能中断试次
        return
    facts = environment_facts(result.stdout or "", "runtime") if result.return_code == 0 else None
    if facts is not None:
        _write(agent_dir, facts)


def _task_image(trial_dir: Path) -> str | None:
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    task = Path(result["config"]["task"]["path"]) / "task.toml"
    if not task.is_file():
        return None
    image: Any = tomllib.loads(task.read_text(encoding="utf-8")).get("environment", {})
    return image.get("docker_image") if isinstance(image, dict) else None


def probe_image(image: str) -> str | None:
    """在断网、用完即删的容器里对任务镜像执行同一探测。"""
    command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c"]
    try:
        done = subprocess.run(
            [*command, PROBE], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def backfill(run_dir: Path, probe: Callable[[str], str | None] = probe_image) -> dict[str, int]:
    """只补没有记录的已结束试次；每个镜像只探测一次。"""
    counts = {"recorded": 0, "backfilled": 0, "unknown": 0}
    probed: dict[str, dict[str, str] | None] = {}
    for trial_dir in sorted(run_dir.glob("jobs/*/*/")):
        if not (trial_dir / "result.json").is_file():
            continue
        if (trial_dir / "agent" / ENVIRONMENT_FILE).is_file():
            counts["recorded"] += 1
            continue
        image = _task_image(trial_dir)
        if image is not None and image not in probed:
            output = probe(image)
            probed[image] = None if output is None else environment_facts(output, "task_image")
        facts = probed.get(image) if image is not None else None
        if facts is None:
            counts["unknown"] += 1
            continue
        _write(trial_dir / "agent", facts)
        counts["backfilled"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补读已结束试次的任务环境系统信息")
    parser.add_argument("--run-dir", type=Path, required=True)
    counts = backfill(parser.parse_args(argv).run_dir)
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
