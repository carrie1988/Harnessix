"""智能体运行期间断开任务容器的公网，结束后恢复；拓扑见compose/network-isolation.yaml。

只在智能体运行期间断网：安装（mini-swe-agent从包镜像安装）与判分（判分脚本从包镜像取依赖）仍需出网，
与Harbor按阶段切换网络策略的口径相同。运行前把本试次Compose项目的任务容器（服务main）从全部
非internal网络断开，只剩sandbox，再重新核对，仍连着非internal网络就中止试次，不在可出网的容器里
运行智能体；结束（含失败与超时取消）后按原别名重新连接。经过写入智能体日志目录的
network-isolation.json，供逐试次核对确实断网运行。宿主机上以docker命令行操作，不改任何全局配置。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.environments.base import BaseEnvironment
from harbor.environments.docker.docker import (
    DockerEnvironment,
    _sanitize_docker_compose_project_name,
)
from harbor.models.environment_type import EnvironmentType

EVIDENCE_FILE = "network-isolation.json"
MAIN_SERVICE = "main"
Docker = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class Attachment:
    """容器在一个网络上的连接：网络名、是否internal、需要恢复的别名。"""

    network: str
    internal: bool
    aliases: tuple[str, ...]


async def docker_cli(*arguments: str) -> str:
    """运行一条docker命令并返回标准输出；失败时以RuntimeError报告命令与错误输出末尾。"""
    process = await asyncio.create_subprocess_exec(
        "docker",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(arguments[:2])}失败（{process.returncode}）："
            f"{stderr.decode(errors='replace')[-400:]}"
        )
    return stdout.decode()


async def main_container(project: str, docker: Docker) -> str:
    """本试次Compose项目中运行的main容器ID；不是恰好一个即报错。"""
    output = await docker(
        "ps",
        "--quiet",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={MAIN_SERVICE}",
    )
    containers = output.split()
    if len(containers) != 1:
        raise RuntimeError(f"Compose项目{project}运行中的main容器有{len(containers)}个")
    return containers[0]


async def attachments(container: str, docker: Docker) -> list[Attachment]:
    """容器当前连接的网络；别名不含Docker按容器短ID自动添加的那个。"""
    endpoints = json.loads(
        await docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container)
    )
    result = []
    for network, endpoint in sorted((endpoints or {}).items()):
        internal = json.loads(
            await docker("network", "inspect", "--format", "{{json .Internal}}", network)
        )
        aliases = tuple(
            alias
            for alias in (endpoint or {}).get("Aliases") or ()
            if not container.startswith(alias)
        )
        result.append(Attachment(network, internal is True, aliases))
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write(logs_dir: Path, record: dict[str, Any]) -> None:
    (logs_dir / EVIDENCE_FILE).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def compose_project(environment: BaseEnvironment) -> str:
    """Harbor Docker环境的Compose项目名；其他环境无法按本拓扑断网，直接拒绝。"""
    if not isinstance(environment, DockerEnvironment) or environment.type() != (
        EnvironmentType.DOCKER
    ):
        raise RuntimeError("智能体断网运行只支持Harbor的Docker环境")
    return _sanitize_docker_compose_project_name(environment.session_id)


@asynccontextmanager
async def offline_agent_run(
    project: str, logs_dir: Path, docker: Docker = docker_cli
) -> AsyncIterator[None]:
    """在上下文内让项目的main容器只连internal网络；离开时恢复断开的网络。"""
    container = await main_container(project, docker)
    before = await attachments(container, docker)
    record: dict[str, Any] = {
        "project": project,
        "container": container[:12],
        "before": [asdict(item) for item in before],
    }
    if not any(item.internal for item in before):
        _write(logs_dir, {**record, "error": "no_internal_network"})
        raise RuntimeError("任务容器没有internal网络：运行说明须加入compose/network-isolation.yaml")
    disconnected: list[Attachment] = []
    try:
        for item in before:
            if not item.internal:
                await docker("network", "disconnect", item.network, container)
                disconnected.append(item)
        during = await attachments(container, docker)
        record.update(isolated_at=_now(), during=[asdict(item) for item in during])
        if not all(item.internal for item in during):
            raise RuntimeError("断开后任务容器仍连接非internal网络")
        _write(logs_dir, record)
        yield
    finally:
        record.update(run_ended_at=_now(), restored=[])
        failures = []
        for item in disconnected:
            aliases = [argument for alias in item.aliases for argument in ("--alias", alias)]
            try:
                await docker("network", "connect", *aliases, item.network, container)
            except RuntimeError as error:
                failures.append(f"{item.network}: {error}")
            else:
                record["restored"].append(item.network)
        record["restore_failures"] = failures
        _write(logs_dir, record)
        if failures:
            raise RuntimeError(f"任务容器网络恢复失败，判分阶段无法出网：{failures}")
