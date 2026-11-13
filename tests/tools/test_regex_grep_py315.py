"""Python 3.15起的正则搜索：regex引擎，按RE2语法拒绝回溯写法，每行限时（Beta Issue #10）。

google-re2没有3.15的预编译包，3.15上安装失败（三个平台相同）。负责人2026-11-13选方案C1：3.15起改用regex。
这里在当前解释器上切换到3.15的路径，经真实的工具Runtime验证；与RE2的一致性在两个引擎都可用时逐行对比。
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from harnessix.tools import grep_matcher
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import RegexGrepInput
from harnessix.tools.workspace import ReadOperation
from tests.tools.test_files import execute

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def python_315(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grep_matcher, "USE_RE2", False)


async def test_search_on_python_315_matches_like_re2(tmp_path: Path, python_315: None) -> None:
    body = b"class SyntheticNode(Base):\r\nclass OtherNode (Base):\r\na+b\r\naaab\r\n"
    (tmp_path / "source.py").write_bytes(body)
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        regex = await execute(tools, "grep", query=r"^class\s+\w+\s*\(")
        literal = await execute(tools, "grep", query="a+b", match_mode="literal")
        plus = await execute(tools, "grep", query="a+b")
    assert [hit["line"] for hit in regex.output["matches"]] == [1, 2]
    assert [hit["line"] for hit in literal.output["matches"]] == [3]
    assert [hit["line"] for hit in plus.output["matches"]] == [4]


PATTERNS = [
    r"def \w+\(",
    r"^\s*import (os|sys)$",
    r"[A-Z][a-z]+Error",
    r"(?i)todo",
    r"\d{2,4}-\d\d",
    r"foo|bar|baz",
    r"(?P<name>x+)y",
    r"\bend\b",
    r"[^\w\s]{2,}",
    r"^$",
]
LINES = [
    "def compute(x):",
    "    import os",
    "import sys",
    "raise ValueError('x')",
    "# TODO: fix",
    "date 2026-11 and 13-05",
    "foo bar",
    "xxxy",
    "the end.",
    "a == b != c",
    "",
    "endless",
]


def test_regex_engine_finds_the_same_first_match_as_re2(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("re2", reason="对比需要google-re2（Python 3.14及以下）")
    for pattern in PATTERNS:
        args = RegexGrepInput(query=pattern)
        re2_matcher = grep_matcher.build_line_matcher(args, ReadOperation())
        with monkeypatch.context() as patch:
            patch.setattr(grep_matcher, "USE_RE2", False)
            regex_matcher = grep_matcher.build_line_matcher(args, ReadOperation())
        assert [regex_matcher(line) for line in LINES] == [re2_matcher(line) for line in LINES], (
            pattern
        )


@pytest.mark.parametrize(
    "query",
    [
        r"(PRIVATE_MARKER)\1",
        "(?=PRIVATE_MARKER)",
        "(?<=PRIVATE_MARKER)x",
        "(?!PRIVATE_MARKER)",
        "(?<!PRIVATE_MARKER)x",
        "(?>PRIVATE_MARKER)",
        "PRIVATE_MARKER++",
        "PRIVATE_MARKER{1001}",
        "(?x)PRIVATE_MARKER",
        "(PRIVATE_MARKER",
    ],
)
async def test_syntax_re2_rejects_is_rejected_on_python_315_too(
    tmp_path: Path, python_315: None, query: str
) -> None:
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        result = await execute(tools, "grep", query=query)
    assert result.outcome == "failed" and result.error.code == "tool_regex_invalid"
    assert "match_mode=literal" in result.error.message
    assert "PRIVATE_MARKER" not in result.model_dump_json()


async def test_a_line_that_takes_too_long_stops_the_search_with_advice(
    tmp_path: Path, python_315: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regex是回溯引擎：这个表达式在200个a上要约0.2秒；限时0.05秒时按“过于复杂”结束，不会卡住。"""
    monkeypatch.setattr(grep_matcher, "REGEX_LINE_TIMEOUT_SECONDS", 0.05)
    (tmp_path / "source.py").write_bytes(b"a" * 200 + b"\n")
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        result = await execute(tools, "grep", query="(.*a){12}x")
    assert result.outcome == "failed" and result.error.code == "tool_regex_timeout"
    assert "match_mode=literal" in result.error.message


async def test_missing_or_drifted_regex_fails_closed_but_literal_still_works(
    tmp_path: Path, python_315: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "source.py").write_bytes(b"needle\n")
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        monkeypatch.setattr(grep_matcher.metadata, "version", lambda _: "9.9.9")
        result = await execute(tools, "grep", query="PRIVATE_MARKER")
        literal = await execute(tools, "grep", query="needle", match_mode="literal")
    assert result.error.code == "tool_regex_unavailable"
    assert "PRIVATE_MARKER" not in result.model_dump_json()
    assert literal.outcome == "succeeded"


def test_the_execution_contract_names_the_engine_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grep_matcher, "USE_RE2", False)
    contract = grep_matcher.execution_contract()
    assert contract["engine"] == "regex" and contract["version"] == grep_matcher.REGEX_VERSION
    assert contract["options"]["line_timeout_seconds"] == grep_matcher.REGEX_LINE_TIMEOUT_SECONDS
    monkeypatch.setattr(grep_matcher, "USE_RE2", True)
    assert grep_matcher.execution_contract()["engine"] == "google-re2"


def test_each_python_version_installs_exactly_one_engine() -> None:
    """3.14及以下装google-re2（不变），3.15及以上装regex；版本与代码里的合同一致。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = {item.split("==")[0]: item for item in project["dependencies"]}
    assert dependencies["google-re2"] == (
        f"google-re2=={grep_matcher.RE2_VERSION}; python_version < '3.15'"
    )
    assert dependencies["regex"] == f"regex=={grep_matcher.REGEX_VERSION}; python_version >= '3.15'"
    installed = metadata.version("regex")
    assert installed == grep_matcher.REGEX_VERSION


@pytest.mark.parametrize(
    ("pattern", "supported"),
    [
        (r"def \w+\(", True),
        (r"(?:a|b)+", True),
        (r"(?P<name>x)", True),
        (r"(?<name>x)", True),
        (r"(?is:x.)", True),
        (r"a{2,1000}", True),
        (r"[(?=]", True),
        (r"\\1", True),
        (r"{not a repeat}", True),
        (r"[^]a]", True),
        (r"\pL+", True),
        (r"(a)\1", False),
        (r"[\]x]\1", False),
        (r"[a]{1001}", False),
        (r"(?=x)", False),
        (r"(?<=x)", False),
        (r"(?>x)", False),
        (r"a*+", False),
        (r"a{2}+", False),
        (r"(?R)", False),
        (r"(?(1)a|b)", False),
        (r"(?#c)", False),
        (r"(?x)a b", False),
        (r"(?P=name)", False),
        (r"\K", False),
        (r"\g<1>", False),
        (r"\k<n>", False),
    ],
)
def test_re2_syntax_check(pattern: str, supported: bool) -> None:
    assert grep_matcher.supported_by_re2(pattern) is supported
