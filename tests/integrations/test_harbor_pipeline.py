"""Harbor运行说明到计划的映射：主指标口径选择计划版本，智能体配置文件与提示模板按内容计入组别摘要。"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("harbor", minversion="0.25.0")

from harbor.utils.templating import render_prompt_template

from integrations.harbor.pipeline import (
    NETWORK_ISOLATION,
    _plan_version,
    _resolve_repository_files,
    check_network_isolation,
)

ROOT = Path(__file__).resolve().parents[2]


def test_primary_outcome_selects_the_plan_version() -> None:
    assert _plan_version({}) == "evalkit.run-plan/v1"
    assert _plan_version({"primary_outcome": "grader_and_normal_end"}) == "evalkit.run-plan/v1"
    assert _plan_version({"primary_outcome": "grader"}) == "evalkit.run-plan/v2"
    with pytest.raises(SystemExit):
        _plan_version({"primary_outcome": "patch_only"})


@pytest.mark.parametrize("key", ["config_file", "prompt_template_path"])
def test_relative_agent_file_resolves_under_code_root_and_becomes_an_artifact(
    tmp_path: Path, key: str
) -> None:
    arm = {"agent": {"kwargs": {key: "configs/r.yaml"}}, "artifacts": ["/b.tar"]}
    _resolve_repository_files(arm, tmp_path)
    assert arm["agent"]["kwargs"][key] == str(tmp_path / "configs/r.yaml")
    assert arm["artifacts"] == ["/b.tar", str(tmp_path / "configs/r.yaml")]
    absolute = {"agent": {"kwargs": {key: "/etc/r.yaml"}}}
    _resolve_repository_files(absolute, tmp_path)
    assert absolute == {"agent": {"kwargs": {key: "/etc/r.yaml"}}}


def test_harnessix_swebench_template_renders_the_issue_verbatim_with_mini_aligned_framing() -> None:
    """评测侧任务说明：Harbor原样嵌入题目，框定只改源码、仓库外复现、通过即结束与逐步说明。"""
    template = ROOT / "benchmarks/harbor/harnessix/swebench-task.j2"
    issue = "Callable storage {{ not a template }} fails\nwith `default_storage` <b>"
    rendered = render_prompt_template(template, issue)
    assert rendered.startswith("<pr_description>\nConsider the following PR description:\n")
    assert f"\n{issue}\n</pr_description>" in rendered
    for phrase in (
        "Change only regular, non-test source files",
        "Do not add or edit repository tests",
        "scratch script outside the test",
        "run the relevant existing test file(s) once",
        "Do not redesign or rewrite a fix that already passes",
        "Add no validation, options or behaviour that the issue does not ask for",
        "already fails\non the original code",
        "first state your current hypothesis or next step in one short line",
    ):
        assert phrase in rendered


@pytest.mark.parametrize(
    "spec",
    [
        "benchmarks/harbor/swebench-verified-holdout49/r-mini-swe-agent.json",
        "benchmarks/harbor/swebench-multilingual-holdout30/r-mini-swe-agent.json",
        "benchmarks/harbor/swebench-verified-holdout49/harnessix-single.json",
        "benchmarks/harbor/swebench-verified-holdout49/harnessix-guard-retry-part2.json",
        "benchmarks/harbor/swebench-verified-e0/e0-full-content.json",
        "benchmarks/harbor/swebench-verified-holdout49/network-isolation-smoke.json",
    ],
)
def test_holdout_harbor_specs_share_the_primary_rule_cap_and_balanced_relay(spec: str) -> None:
    """holdout在Harbor上的组别：主指标只看判分、上限3900秒、经宿主机均衡转发访问模型。"""
    document = json.loads((ROOT / spec).read_text(encoding="utf-8"))
    assert _plan_version(document) == "evalkit.run-plan/v2"
    assert [arm["arm_id"] for arm in document["arms"]][0] == "oracle"
    for arm in document["arms"][1:]:
        agent = arm["agent"]
        assert agent["override_timeout_sec"] == 3900
        kwargs = agent["kwargs"]
        if "config_file" in kwargs:
            assert (ROOT / kwargs["config_file"]).is_file()
            assert "host.docker.internal:18100/v1" in (ROOT / kwargs["config_file"]).read_text()
        else:
            assert kwargs["model_relays"].endswith("=host.docker.internal:18100")
        if "prompt_template_path" in kwargs:
            assert not Path(kwargs["prompt_template_path"]).is_absolute()
            render_prompt_template(ROOT / kwargs["prompt_template_path"], "issue")


def test_every_agent_arm_runs_offline_exactly_when_the_spec_has_the_isolation_overlay() -> None:
    """正式评测把多个组别放在同一运行说明里：断网覆盖对其中每个智能体组别都生效，对照组除外。"""
    isolated = []
    for path in sorted((ROOT / "benchmarks/harbor").glob("*/*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if "arms" not in document:
            continue
        check_network_isolation(document)
        overlays = document.get("environment", {}).get("extra_docker_compose", [])
        if NETWORK_ISOLATION in overlays:
            isolated.append(path.name)
            assert "integrations/harbor/compose/raw-github-mirror.yaml" in overlays
            environment = document["compose_env"]
            assert (ROOT / environment["HARNESSIX_MODEL_GATEWAY_CODE"]).is_file()
            assert not ipaddress.ip_address(environment["HARNESSIX_TASK_DNS"]).is_loopback
    assert isolated


def _spec(overlays: list[str], **flags: bool) -> dict:
    systems = {"oracle": "oracle", "new": "harnessix", "old": "harnessix", "R": "mini-swe-agent"}
    return {
        "environment": {"extra_docker_compose": overlays},
        "arms": [
            {
                "arm_id": arm,
                "system": system,
                "agent": {"kwargs": {"network_isolation": flags[arm]} if arm in flags else {}},
            }
            for arm, system in systems.items()
        ],
    }


def test_a_spec_cannot_leave_one_agent_arm_online_or_reach_the_host_directly() -> None:
    mirror = "integrations/harbor/compose/raw-github-mirror.yaml"
    gateway = "integrations/harbor/compose/host-gateway.yaml"
    offline = [mirror, NETWORK_ISOLATION]
    check_network_isolation(_spec(offline, new=True, old=True, R=True))
    check_network_isolation(_spec([mirror, gateway]))
    for spec in (
        _spec(offline, new=True, old=True),
        _spec(offline, new=True, old=True, R=True, oracle=True),
        _spec([mirror], R=True),
        _spec([*offline, gateway], new=True, old=True, R=True),
    ):
        with pytest.raises(SystemExit):
            check_network_isolation(spec)


def test_isolation_overlay_leaves_main_only_the_two_sidecars_once_detached() -> None:
    compose = yaml.safe_load((ROOT / NETWORK_ISOLATION).read_text(encoding="utf-8"))
    sandbox = compose["networks"]["sandbox"]
    assert sandbox["internal"] is True
    assert sandbox["driver_opts"]["com.docker.network.bridge.inhibit_ipv4"] == "true"
    services = compose["services"]
    assert set(services["main"]["networks"]) == {"default", "sandbox"}
    assert services["main"]["dns"] == ["${HARNESSIX_TASK_DNS:?required}"]
    assert "extra_hosts" not in services["main"]
    gateway = services["model-gateway"]
    assert gateway["networks"]["sandbox"]["aliases"] == ["host.docker.internal"]
    assert gateway["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert "0.0.0.0:18100=host.docker.internal:18100" in gateway["command"]
    assert "--container" in gateway["command"] and "ports" not in gateway
    mirror = services["raw-github-mirror"]["networks"]["sandbox"]["aliases"]
    assert mirror == ["raw.githubusercontent.com"]
