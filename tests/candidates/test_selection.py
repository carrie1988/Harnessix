from __future__ import annotations

import pytest

from harnessix.candidates.selection import (
    Candidate,
    Check,
    CommandRecord,
    is_discriminating,
    rank,
    select_checks,
)

PATCH_A = "diff --git a/x.py b/x.py\n+fix A\n"
PATCH_B = "diff --git a/x.py b/x.py\n+fix B longer\n"


def _ok(cmd: str) -> CommandRecord:
    return CommandRecord(cmd, "succeeded", 0)


def _fail(cmd: str) -> CommandRecord:
    return CommandRecord(cmd, "failed", 1)


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("export HOME=/tmp && cd /testbed && python -m pytest tests/test_x.py -q", True),
        ("cd /testbed && python tests/runtests.py httpwrappers", True),
        ("npm test", True),
        # 个别仓库自带的Runner不再特殊识别；经python启动时仍按通用规则计入。
        ("cd /testbed && ./tests/runtests.py httpwrappers", False),
        ("bin/test sympy/core", False),
        ("python -c 'import x; assert x.f() == 1'", True),
        ("go test ./pkg/...", True),
        ("cat django/http/response.py", False),
        ("cd /testbed && grep -rn memoryview django", False),
        ("git diff", False),
        ("ls tests", False),
    ],
)
def test_only_commands_that_exercise_code_discriminate(cmd: str, expected: bool) -> None:
    assert is_discriminating(cmd) is expected


def test_checks_are_last_successful_runs_taken_round_robin_without_duplicates() -> None:
    a = Candidate(
        "a",
        PATCH_A,
        "completed",
        (
            _ok("pytest t1"),
            _fail("pytest t2"),  # 自己的补丁上失败：不能作为“正确补丁应通过”的依据
            _ok("cat x.py"),
            _fail("pytest t1"),
            _ok("pytest t1"),  # 最后一次通过
            _ok("pytest t3"),
        ),
    )
    b = Candidate("b", PATCH_B, "completed", (_ok("pytest t3"), _ok("python repro.py")))
    empty = Candidate("c", "", "completed", (_ok("pytest t9"),))

    checks = select_checks([a, b, empty])

    assert checks == (
        Check("pytest t3", "a"),
        Check("python repro.py", "b"),
        Check("pytest t1", "a"),
    )
    assert len(select_checks([a, b], limit=2)) == 2


def test_rank_prefers_passed_checks_then_agreement_completion_and_size() -> None:
    a = Candidate("a", PATCH_A, "failed")
    b = Candidate("b", PATCH_B, "completed")
    b_twin = Candidate("b2", PATCH_B, "completed")
    empty = Candidate("e", "", "completed")
    checks = (Check("pytest t1", "a"), Check("pytest t2", "b"))

    by_tests = rank(
        [a, b, b_twin, empty],
        checks,
        {"a": {0: "passed", 1: "passed"}, "b": {0: "passed", 1: "failed"}, "b2": {0: "failed"}},
    )
    assert by_tests.selected == "a"
    assert [item.candidate_id for item in by_tests.ranking] == ["a", "b", "b2"]
    assert "通过2/2条" in by_tests.reason

    tied = rank([a, b, b_twin], checks, {})
    # 无检查区分时，两个候选给出相同补丁的一方胜出。
    assert tied.selected == "b" and tied.ranking[0].duplicates == 2

    assert rank([empty], checks, {}).selected is None
