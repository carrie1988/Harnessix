"""智能体运行期间断网：只剩internal网络才运行智能体，结束（含失败、超时）后按原别名恢复。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("harbor", minversion="0.25.0")

from harbor.agents.installed.mini_swe_agent import MiniSweAgent
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.agent.context import AgentContext

from integrations.harbor import evidence_agents
from integrations.harbor.network_isolation import (
    EVIDENCE_FILE,
    compose_project,
    offline_agent_run,
)

PROJECT = "django__django-15731__abc__env"
MAIN = "f" * 64


class FakeDocker:
    """按docker命令行的输入输出模拟一个Compose项目里main容器的网络连接。"""

    def __init__(self, networks: dict[str, bool], *, fail: set[object] | None = None) -> None:
        self.internal = networks
        self.attached = {
            name: {"Aliases": [f"{PROJECT}-main-1", "main", MAIN[:12]]} for name in networks
        }
        self.containers = [MAIN]
        self.fail = fail or set()
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *arguments: str) -> str:
        self.calls.append(arguments)
        command = " ".join(arguments[:2])
        if command in self.fail or (command, arguments[-2]) in self.fail:
            raise RuntimeError(f"docker {command}失败")
        if arguments[0] == "ps":
            assert f"label=com.docker.compose.project={PROJECT}" in arguments
            assert "label=com.docker.compose.service=main" in arguments
            return "".join(f"{container}\n" for container in self.containers)
        if arguments[0] == "inspect":
            return json.dumps(self.attached)
        if command == "network inspect":
            return json.dumps(self.internal[arguments[-1]])
        if command == "network disconnect":
            del self.attached[arguments[2]]
            return ""
        if command == "network connect":
            aliases = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--alias"]
            self.attached[arguments[-2]] = {"Aliases": [*aliases, MAIN[:12]]}
            return ""
        raise AssertionError(arguments)

    def mutations(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[1] in {"connect", "disconnect"}]


def evidence(tmp_path: Path) -> dict:
    return json.loads((tmp_path / EVIDENCE_FILE).read_text(encoding="utf-8"))


async def test_the_agent_runs_only_on_internal_networks_and_gets_its_networks_back(
    tmp_path: Path,
) -> None:
    docker = FakeDocker({"p_default": False, "p_sandbox": True})
    async with offline_agent_run(PROJECT, tmp_path, docker):
        assert set(docker.attached) == {"p_sandbox"}
        during = evidence(tmp_path)
        assert [item["network"] for item in during["during"]] == ["p_sandbox"]
        assert "restored" not in during
    assert set(docker.attached) == {"p_default", "p_sandbox"}
    assert docker.mutations() == [
        ("network", "disconnect", "p_default", MAIN),
        ("network", "connect", "--alias", f"{PROJECT}-main-1", "--alias", "main")
        + ("p_default", MAIN),
    ]
    record = evidence(tmp_path)
    assert record["container"] == MAIN[:12]
    assert record["restored"] == ["p_default"] and record["restore_failures"] == []
    assert record["isolated_at"] <= record["run_ended_at"]


async def test_failed_or_timed_out_agents_still_restore_the_network_for_the_verifier(
    tmp_path: Path,
) -> None:
    docker = FakeDocker({"p_default": False, "p_sandbox": True})
    with pytest.raises(ValueError):
        async with offline_agent_run(PROJECT, tmp_path, docker):
            raise ValueError("agent failed")
    assert set(docker.attached) == {"p_default", "p_sandbox"}

    async def endless() -> None:
        async with offline_agent_run(PROJECT, tmp_path, docker):
            await asyncio.sleep(3600)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(endless(), timeout=0.05)
    assert set(docker.attached) == {"p_default", "p_sandbox"}
    assert evidence(tmp_path)["restored"] == ["p_default"]


async def test_a_trial_without_the_internal_network_never_starts_the_agent(
    tmp_path: Path,
) -> None:
    docker = FakeDocker({"p_default": False})
    with pytest.raises(RuntimeError, match="network-isolation.yaml"):
        async with offline_agent_run(PROJECT, tmp_path, docker):
            pytest.fail("智能体不应在可出网的容器里运行")
    assert docker.mutations() == []
    assert evidence(tmp_path)["error"] == "no_internal_network"


async def test_a_network_that_cannot_be_detached_stops_the_trial_before_the_agent(
    tmp_path: Path,
) -> None:
    docker = FakeDocker(
        {"p_default": False, "p_extra": False, "p_sandbox": True},
        fail={("network disconnect", "p_extra")},
    )
    with pytest.raises(RuntimeError, match="disconnect"):
        async with offline_agent_run(PROJECT, tmp_path, docker):
            pytest.fail("仍连着可出网网络时不应运行智能体")
    assert set(docker.attached) == {"p_default", "p_extra", "p_sandbox"}
    assert evidence(tmp_path)["restored"] == ["p_default"]


async def test_a_network_that_cannot_be_restored_fails_the_trial_loudly(tmp_path: Path) -> None:
    docker = FakeDocker({"p_default": False, "p_sandbox": True}, fail={"network connect"})
    with pytest.raises(RuntimeError, match="恢复失败"):
        async with offline_agent_run(PROJECT, tmp_path, docker):
            pass
    record = evidence(tmp_path)
    assert record["restored"] == [] and record["restore_failures"][0].startswith("p_default")


@pytest.mark.parametrize("containers", [[], [MAIN, "e" * 64]])
async def test_the_trial_must_have_exactly_one_running_main_container(
    tmp_path: Path, containers: list[str]
) -> None:
    docker = FakeDocker({"p_default": False, "p_sandbox": True})
    docker.containers = containers
    with pytest.raises(RuntimeError, match="main容器"):
        async with offline_agent_run(PROJECT, tmp_path, docker):
            pytest.fail("找不到唯一的任务容器时不应运行智能体")
    assert docker.mutations() == []


def test_compose_project_follows_harbor_and_rejects_other_environments() -> None:
    environment = object.__new__(DockerEnvironment)
    environment.session_id = "Django__Django-15731.x__env"
    assert compose_project(environment) == "django__django-15731-x__env"
    with pytest.raises(RuntimeError, match="Docker"):
        compose_project(SimpleNamespace(session_id="x", type=lambda: "docker"))


@pytest.mark.parametrize("enabled", [False, True])
async def test_mini_runs_offline_only_when_enabled_and_installs_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def offline(project: str, logs_dir: Path):
        events.append(f"offline {project} {logs_dir == tmp_path}")
        yield
        events.append("online")

    async def mini(self, instruction, environment, context) -> None:
        events.append("mini")

    monkeypatch.setattr(evidence_agents, "offline_agent_run", offline)
    monkeypatch.setattr(evidence_agents, "compose_project", lambda environment: "trial-project")
    monkeypatch.setattr(MiniSweAgent, "run", mini)
    agent = evidence_agents.MiniSweAgentOffline(
        logs_dir=tmp_path, model_name="openai/qwen3.8-flash-next", network_isolation=enabled
    )
    assert agent.options.network_isolation is enabled
    environment = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(return_code=1, stdout="", stderr=""))
    )
    await agent.run("fix it", environment, AgentContext())
    assert events == (["offline trial-project True", "mini", "online"] if enabled else ["mini"])
