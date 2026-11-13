"""共享逐行匹配核心；RE2缺失或漂移时失败关闭，不回退到回溯引擎。

google-re2没有Python 3.15的预编译包（Beta Issue #10，负责人2026-11-13选方案C1）：3.15起改用regex，
按RE2的语法拒绝回溯引擎才有的写法，并给每行的匹配限时，超时按表达式过于复杂报错。3.14及以下不变。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from importlib import metadata

from harnessix.agent.errors import KernelError
from harnessix.tools.search_contracts import GrepInput, GrepSnapshotInput, RegexGrepInput
from harnessix.tools.workspace import ReadOperation

RE2_VERSION = "1.1.20251105"
RE2_MAX_MEM = 1024 * 1024
USE_RE2 = sys.version_info < (3, 15)
REGEX_VERSION = "2026.11.9"
# regex是回溯引擎：一行超过这个时间仍未匹配完，按表达式过于复杂停止整次搜索（RE2本身是线性时间）。
REGEX_LINE_TIMEOUT_SECONDS = 0.25
# RE2对计数重复的上限。
RE2_MAX_REPEAT = 1000
LineMatcher = Callable[[str], int]


class RegexSearchError(KernelError):
    """仅传播固定错误码及修正建议，不保存表达式或第三方异常。"""


_INVALID = (
    "表达式不是受支持的RE2模式或超过编译内存上限；请修正语法、移除反向引用/"
    "前后查找断言、简化表达式，或显式使用match_mode=literal查找字面量"
)


def execution_contract() -> dict[str, object]:
    if not USE_RE2:
        return _regex_execution_contract()
    return {
        "engine": "google-re2",
        "version": RE2_VERSION,
        "options": {
            "max_mem": RE2_MAX_MEM,
            "log_errors": False,
            "case_sensitive": True,
            "never_capture": True,
        },
        "match_mode": "explicit-regex-or-literal-default-regex",
        "literal_fallback": "regex-with-metacharacters-and-no-match-retries-as-literal",
        "semantics": "RE2.search-first-match-per-line-no-pattern-rewriting",
        "text": "UTF-8-no-normalization-CRLF-stripped-for-matching-only",
        "unsupported": "backreferences-and-lookaround-rejected",
        "compile": "once-per-call-before-file-scan",
        # 官方compile的LRU最多128项；每项RE2预算1 MiB，合计最多128 MiB。
        # 仅使用公开API，不绕过或清理该进程共享缓存。
        "engine_cache_entries": 128,
        "engine_cache_max_mem": 128 * RE2_MAX_MEM,
    }


def _regex_execution_contract() -> dict[str, object]:
    return {
        "engine": "regex",
        "version": REGEX_VERSION,
        "options": {
            "flags": "VERSION0",
            "case_sensitive": True,
            "line_timeout_seconds": REGEX_LINE_TIMEOUT_SECONDS,
        },
        "match_mode": "explicit-regex-or-literal-default-regex",
        "literal_fallback": "regex-with-metacharacters-and-no-match-retries-as-literal",
        "semantics": "RE2-syntax-subset-first-match-per-line-unicode-perl-classes",
        "text": "UTF-8-no-normalization-CRLF-stripped-for-matching-only",
        "unsupported": (
            "backreferences-lookaround-atomic-possessive-recursion-conditionals-comments-"
            "verbose-and-repeats-over-1000-rejected"
        ),
        "compile": "once-per-call-before-file-scan",
        "timeout": "per-line-stops-the-search",
    }


_REGEX_METACHARACTERS = frozenset(".^$*+?()[]{}|\\")


def literal_fallback_args(args: GrepInput) -> RegexGrepInput | None:
    """正则模式下查询含元字符时给出同一查询的字面量参数，供完整扫描无命中后重搜；否则为None。

    模型常把含括号、点号的源码片段当字面量查找，按正则解释后静默无命中。
    """
    if (
        isinstance(args, RegexGrepInput)
        and args.match_mode == "regex"
        and any(character in _REGEX_METACHARACTERS for character in args.query)
    ):
        return args.model_copy(update={"match_mode": "literal"})
    return None


# 行首、行尾与文本首尾锚点：逐行匹配时每行各算一次，整份文本只算一次，两者结论可能不同。
_LINE_ANCHORS = ("^", "$", "\\A", "\\z")


def whole_text_decides(args: GrepInput) -> bool:
    """整份文本不命中时，能否断定其中任何一行都不命中（快照与正则搜索）。

    字面量查询可以：查询不含控制字符，命中不会跨行。不含上述锚点的正则也可以：RE2没有前后查找，
    一行内的命中在整份文本的同一位置同样成立（单词边界在行首行尾两侧的结论相同）。
    含锚点的正则不能，只能逐行判断。原字面量合同不使用这条捷径，扫描与统计口径保持不变。
    """
    if not isinstance(args, GrepSnapshotInput):
        return False
    if isinstance(args, RegexGrepInput) and args.match_mode == "regex":
        return not any(anchor in args.query for anchor in _LINE_ANCHORS)
    return True


def build_line_matcher(args: GrepInput, operation: ReadOperation) -> LineMatcher:
    if not isinstance(args, RegexGrepInput) or args.match_mode == "literal":
        return lambda line: line.find(args.query)

    operation.checkpoint()
    if not USE_RE2:
        return _regex_line_matcher(args.query, operation)
    unavailable = "RE2引擎不可用；安装合同指定的google-re2版本后重试，不回退到其他引擎"
    try:
        if metadata.version("google-re2") != RE2_VERSION:
            raise RegexSearchError("tool_regex_unavailable", unavailable)
        re2 = importlib.import_module("re2")
        options = re2.Options()
        options.max_mem = RE2_MAX_MEM
        options.log_errors = False
        options.case_sensitive = True
        options.never_capture = True
    except (metadata.PackageNotFoundError, ImportError, OSError, AttributeError):
        raise RegexSearchError("tool_regex_unavailable", unavailable) from None
    try:
        compiled = re2.compile(args.query, options=options)
    except re2.error:
        raise RegexSearchError("tool_regex_invalid", _INVALID) from None
    operation.checkpoint()

    def first_match_index(line: str) -> int:
        match = compiled.search(line)
        return -1 if match is None else int(match.start())

    return first_match_index


def _regex_line_matcher(query: str, operation: ReadOperation) -> LineMatcher:
    """Python 3.15起的匹配：先按RE2语法拒绝，再用regex编译；每行限时。"""

    unavailable = "regex引擎不可用；安装合同指定的regex版本后重试，不回退到其他引擎"
    try:
        if metadata.version("regex") != REGEX_VERSION:
            raise RegexSearchError("tool_regex_unavailable", unavailable)
        engine = importlib.import_module("regex")
    except (metadata.PackageNotFoundError, ImportError, OSError):
        raise RegexSearchError("tool_regex_unavailable", unavailable) from None
    if not supported_by_re2(query):
        raise RegexSearchError("tool_regex_invalid", _INVALID)
    try:
        compiled = engine.compile(query, flags=engine.VERSION0)
    except (engine.error, OverflowError, RecursionError):
        raise RegexSearchError("tool_regex_invalid", _INVALID) from None
    operation.checkpoint()

    def first_match_index(line: str) -> int:
        try:
            match = compiled.search(line, timeout=REGEX_LINE_TIMEOUT_SECONDS)
        except TimeoutError:
            raise RegexSearchError(
                "tool_regex_timeout",
                "表达式在一行上匹配超时（回溯过多）；请简化表达式，避免嵌套的量词（如(a+)+）"
                "与可以互相重叠的重复，或显式使用match_mode=literal查找字面量",
            ) from None
        return -1 if match is None else int(match.start())

    return first_match_index


# (?之后RE2接受的写法：非捕获组、命名组与内联标志（i、m、s，可带-与:）。
_RE2_GROUP_FLAGS = frozenset("ims-:)")


def supported_by_re2(pattern: str) -> bool:
    """回溯引擎才有、RE2不接受的写法返回False。

    逐字符扫描，跳过转义与字符类内部：反向引用（\\1、\\g、\\k）、\\K、前后查找、原子组、占有量词、
    递归、条件分支、注释组、RE2没有的内联标志（如x、a），以及超过1000的计数重复。
    """
    index, in_class = 0, False
    while index < len(pattern):
        char = pattern[index]
        following = pattern[index + 1 : index + 2]
        if char == "\\":
            if not in_class and following and following in "gkK123456789":
                return False
            index += 2
            continue
        if in_class:
            in_class = char != "]"
            index += 1
            continue
        if char == "[":
            index += 1
            index += pattern[index : index + 1] == "^"
            index += pattern[index : index + 1] == "]"
            in_class = True
            continue
        if char == "(" and following == "?" and not _supported_group(pattern[index + 2 :]):
            return False
        if char in "*+?}" and following == "+":
            return False
        if char == "{" and not _supported_repeat(pattern[index + 1 :]):
            return False
        index += 1
    return True


def _supported_group(rest: str) -> bool:
    if rest.startswith(("P<", "<")) and not rest.startswith(("<=", "<!")):
        return True
    flags = rest[: next((i for i, c in enumerate(rest) if c in ":)"), len(rest)) + 1]
    return bool(flags) and all(c in _RE2_GROUP_FLAGS for c in flags)


def _supported_repeat(rest: str) -> bool:
    """{n}、{n,}、{n,m}的计数不超过RE2的上限；不是计数重复的花括号按字面量放行。"""
    body = rest.split("}", 1)[0]
    counts = body.split(",")
    if "}" not in rest or len(counts) > 2 or not counts[0].isdigit():
        return True
    return all(int(count) <= RE2_MAX_REPEAT for count in counts if count.isdigit())
