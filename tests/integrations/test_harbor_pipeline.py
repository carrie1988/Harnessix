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
    ],
)
def test_holdout_reference_specs_use_the_grader_only_primary_and_r_config(spec: str) -> None:
    document = json.loads((ROOT / spec).read_text(encoding="utf-8"))
    assert _plan_version(document) == "evalkit.run-plan/v2"
    reference = next(arm for arm in document["arms"] if arm["arm_id"] == "R")
    assert reference["agent"]["override_timeout_sec"] == 3900
    assert (ROOT / reference["agent"]["kwargs"]["config_file"]).is_file()
