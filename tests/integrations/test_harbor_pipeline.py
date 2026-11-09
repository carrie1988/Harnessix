"""Harbor运行说明到计划的映射：主指标口径选择计划版本，智能体配置文件与提示模板按内容计入组别摘要。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("harbor", minversion="0.25.0")

from harbor.utils.templating import render_prompt_template

from integrations.harbor.pipeline import _plan_version, _resolve_repository_files

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
        if "prompt_template_path" in kwargs:
            assert not Path(kwargs["prompt_template_path"]).is_absolute()
            render_prompt_template(ROOT / kwargs["prompt_template_path"], "issue")
