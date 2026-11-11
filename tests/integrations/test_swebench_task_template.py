"""评测任务模板v4（ADR 0134）的固定文字；不依赖Harbor，渲染由test_harbor_pipeline覆盖。"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "benchmarks/harbor/harnessix"
PLACEHOLDER = "{{ instruction }}"


def _issue_wrapper_and_instructions(name: str) -> tuple[str, str]:
    wrapper, separator, instructions = (
        (TEMPLATES / name).read_text(encoding="utf-8").partition("<instructions>\n")
    )
    assert separator and instructions.endswith("</instructions>\n")
    return wrapper, instructions.removesuffix("</instructions>\n")


def test_v4_embeds_the_issue_exactly_as_v3_does() -> None:
    """两版只在说明部分不同，题目的嵌入方式相同，评测结果才能归因于说明。"""
    wrapper, instructions = _issue_wrapper_and_instructions("swebench-task-v4.j2")
    assert wrapper == _issue_wrapper_and_instructions("swebench-task-v3.j2")[0]
    assert wrapper.count(PLACEHOLDER) == 1
    assert "{" not in instructions and "}" not in instructions


def test_v4_states_only_what_the_benchmark_itself_imposes() -> None:
    """工作习惯是产品默认指令；模板只剩任务与这个评测自身的事实，再写做法就会与产品指令重复或冲突。"""
    instructions = " ".join(_issue_wrapper_and_instructions("swebench-task-v4.j2")[1].split())
    for fact in (
        "Resolve the issue in the PR description by changing the repository in the current "
        "workspace.",
        "Your answer is the diff between the workspace as you found it and as you leave it, "
        "including new files.",
        "Only changes to regular, non-test source files count.",
        "The evaluation runs its own tests in the workspace you leave, with the build, "
        "dependency and tool configuration already there",
        "so tests and that configuration must end up as you found them.",
    ):
        assert fact in instructions, fact
    # 多语言评测共用：配置文件举例不偏向一种语言。
    for manifest in ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml"):
        assert manifest in instructions
    assert len(instructions) <= 700
    lowered = instructions.lower()
    for working_rule in (
        "reproduce",
        "scratch",
        "/tmp",
        "verify",
        "workflow",
        "stash",
        "regression",
        "variant",
        "focused",
        "root cause",
        "consistent with",
        "hypothesis",
        "summary",
        "delete",
        "python",
    ):
        assert working_rule not in lowered, working_rule
