"""Harbor运行说明到计划的映射：主指标口径选择计划版本，智能体配置文件按内容计入组别摘要。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("harbor", minversion="0.25.0")

from integrations.harbor.pipeline import _plan_version, _resolve_config_file

ROOT = Path(__file__).resolve().parents[2]


def test_primary_outcome_selects_the_plan_version() -> None:
    assert _plan_version({}) == "evalkit.run-plan/v1"
    assert _plan_version({"primary_outcome": "grader_and_normal_end"}) == "evalkit.run-plan/v1"
    assert _plan_version({"primary_outcome": "grader"}) == "evalkit.run-plan/v2"
    with pytest.raises(SystemExit):
        _plan_version({"primary_outcome": "patch_only"})


def test_relative_config_file_resolves_under_code_root_and_becomes_an_artifact(
    tmp_path: Path,
) -> None:
    arm = {"agent": {"kwargs": {"config_file": "configs/r.yaml"}}, "artifacts": ["/b.tar"]}
    _resolve_config_file(arm, tmp_path)
    assert arm["agent"]["kwargs"]["config_file"] == str(tmp_path / "configs/r.yaml")
    assert arm["artifacts"] == ["/b.tar", str(tmp_path / "configs/r.yaml")]
    absolute = {"agent": {"kwargs": {"config_file": "/etc/r.yaml"}}}
    _resolve_config_file(absolute, tmp_path)
    assert absolute == {"agent": {"kwargs": {"config_file": "/etc/r.yaml"}}}


@pytest.mark.parametrize(
    "spec",
    [
        "benchmarks/harbor/swebench-verified-holdout49/r-mini-swe-agent.json",
        "benchmarks/harbor/swebench-multilingual-holdout30/r-mini-swe-agent.json",
        "benchmarks/harbor/swebench-verified-holdout49/harnessix-single.json",
        "benchmarks/harbor/swebench-verified-e0/e0-full-content.json",
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
