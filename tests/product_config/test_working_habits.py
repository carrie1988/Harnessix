"""默认工作习惯（ADR 0134）：三种命令执行方式共用一份，对任何用户任务成立，不含评测专用规则。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import ContextBuildInput
from harnessix.product_config.agent_context import (
    _EXTERNAL_BOUNDARY_INSTRUCTIONS,
    _GENERIC_EXECUTION_INSTRUCTIONS,
    _NATIVE_EXECUTION_INSTRUCTIONS,
    _WORKING_HABITS,
    build_product_agent_context,
)

HABITS_SOURCE = "harnessix.working-habits/v2"
# 指令按100列折行，断言按空白归一后的句子比较，不绑定折行位置。
HABITS = " ".join(_WORKING_HABITS.content.split())
MODEL_FACING_TEXTS = (
    HABITS,
    *(
        " ".join(text.split())
        for text in (
            _GENERIC_EXECUTION_INSTRUCTIONS,
            _NATIVE_EXECUTION_INSTRUCTIONS,
            _EXTERNAL_BOUNDARY_INSTRUCTIONS,
        )
    ),
)


async def _runtime_sources(root: Path, **options: bool) -> list[str]:
    context = build_product_agent_context(root, max_output_tokens=4096, **options)
    request = ContextBuildInput(
        thread_id=uuid4(), turn_id=uuid4(), model_step=1, workspace=str(root)
    )
    prepared = await context.context.prepare(request, CancelToken())
    fragments = json.loads(prepared.instructions or "")["fragments"]
    return [item["source"] for item in fragments if item["kind"] == "runtime_instruction"]


@pytest.mark.parametrize(
    ("backend", "execution_source"),
    [
        ({}, "harnessix.generic-execution-instructions/v6"),
        ({"native_execution": True}, "harnessix.native-execution-instructions/v5"),
        (
            {"external_boundary_execution": True},
            "harnessix.external-boundary-execution-instructions/v3",
        ),
    ],
)
async def test_every_command_execution_mode_reads_the_same_habits_after_its_instructions(
    tmp_path: Path, backend: dict[str, bool], execution_source: str
) -> None:
    """产品入口同时开启规划与正则检索；无论还有哪些指引，工具与环境说明之后紧跟工作习惯。"""
    sources = await _runtime_sources(
        tmp_path, generic_execution=True, enable_planning=True, enable_regex_search=True, **backend
    )
    assert sources.count(HABITS_SOURCE) == 1
    assert sources[sources.index(execution_source) + 1] == HABITS_SOURCE
    assert _WORKING_HABITS.source == HABITS_SOURCE


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"enable_planning": True, "enable_regex_search": True},
        {"require_pre_edit_profile": False, "compact_instructions": True},
    ],
)
async def test_fixed_stage_workflow_without_command_tools_gets_no_habits(
    tmp_path: Path, options: dict[str, bool]
) -> None:
    assert HABITS_SOURCE not in await _runtime_sources(tmp_path, **options)


def test_habits_stay_under_twenty_lines_and_hold_for_any_user_task() -> None:
    text = _WORKING_HABITS.content
    assert text.isascii()
    assert len(text.encode()) <= 1800 and text.count("\n") <= 20
    lowered = text.lower()
    for evaluation_only in (
        "benchmark",
        "issue",
        "pr description",
        "pull request",
        "hidden",
        "evaluat",
        "grade",
        "submit",
    ):
        assert evaluation_only not in lowered, evaluation_only


def test_habits_cover_hypothesis_reproduction_regression_consistency_stopping_and_cleanup() -> None:
    for habit in (
        "In every response that calls tools, first state your current hypothesis or next step "
        "in one short line.",
        "Group independent read-only lookups: several tool calls in one response, or several "
        "commands in one shell call.",
        "When the code and the report make the cause clear, change the code first and verify "
        "after.",
        "Otherwise reproduce the problem first, in the cheapest way that runs the affected code "
        "under the project's default configuration",
        "After your change, rerun the reported case and the existing tests nearest to the code "
        "you changed.",
        "it is a regression caused by your change unless you have seen it fail without your "
        "change: correct the change, not the test.",
        "Prefer the change that matches how the codebase already handles similar cases",
        "Fix the root cause, not only the reported example, and add nothing unrelated.",
        "the fix is done: do not extend or rewrite it for cases nobody asked for",
        "Do not finish while your own latest check still shows the reported problem.",
        "never revert changes that were there before you started.",
    ):
        assert habit in HABITS, habit


def test_habits_do_not_excuse_a_broken_existing_test() -> None:
    """v1的例外条款（“除非用户明确要求这种行为变化”）被用来把弄坏的原有测试认定为预期。"""
    assert "unless the user explicitly asked for that behaviour change" not in HABITS


def test_code_reachable_only_through_the_project_test_setup_can_be_reproduced_in_place() -> None:
    """只能经项目自身测试环境到达的代码（包内接口、浏览器里跑的测试、离线依赖），在仓库外的临时程序里
    跑不起来或走不到出问题的那一层。复现放在哪里由代价和是否走到真实代码决定；代价高时允许先改后验。"""
    assert (
        "a temporary test in the project's own test runner or a small script, inside or outside "
        "the workspace" in HABITS
    )
    assert "If that is costly, edit first and verify after." in HABITS
    for text in MODEL_FACING_TEXTS:
        for confining in ("keep scratch files", "outside the test suite", "repository tests"):
            assert confining not in text.lower(), confining


def test_cleanup_is_limited_to_what_the_agent_created() -> None:
    """“让最终diff只剩自己的源码改动”曾让模型还原开始前就存在的改动；清理只针对自己为排查留下的东西。

    v2要求结束前复查整份改动：开发集里13个步数用尽的未解决试次有6个把临时测试或调试输出留在了补丁里。
    """
    assert (
        "review your whole change (for example git status and git diff) and remove what served "
        "only your investigation: temporary files, debug output and experimental edits. Leave "
        "files you did not create or change as they are" in HABITS
    )
    for text in MODEL_FACING_TEXTS:
        for sweeping in ("final diff", "build outputs", "only your source change"):
            assert sweeping not in text.lower(), sweeping


def test_scope_is_anchored_on_existing_handling_instead_of_on_keeping_old_behaviour() -> None:
    """“不加无关校验或选项”“未被要求的行为变化不算理由”曾被用来只照搬同类实现的一半、把正确修复改小。
    改动范围以代码库里同类情况的做法为锚点；回归判断只针对改动后新失败的测试。"""
    assert "(a sibling option, function or command, an existing convention)" in HABITS
    assert "and match it completely." in HABITS
    assert HABITS.count("regression") == 1
    for shrinking in ("unrelated validation", "narrow", "minimal", "smallest", "never justifies"):
        assert shrinking not in HABITS.lower(), shrinking


def test_verification_ends_at_the_reported_case_and_the_nearest_tests() -> None:
    """“把相关变体也验证一遍”曾让模型为没人提出的情况继续改动已经正确的修复，改坏后也没发现。"""
    assert (
        "When the reported case works and those tests pass, the fix is done: do not extend or "
        "rewrite it for cases nobody asked for; mention them in your final message instead."
        in HABITS
    )
    for widening in ("variant", "other call paths", "edge case"):
        assert widening not in HABITS.lower(), widening
