"""共享逐行匹配核心；RE2缺失或漂移时失败关闭，不回退到回溯引擎。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib import metadata

from harnessix.agent.errors import KernelError
from harnessix.tools.search_contracts import GrepInput, GrepSnapshotInput, RegexGrepInput
from harnessix.tools.workspace import ReadOperation

RE2_VERSION = "1.1.20251105"
RE2_MAX_MEM = 1024 * 1024
LineMatcher = Callable[[str], int]


class RegexSearchError(KernelError):
    """仅传播固定错误码及修正建议，不保存表达式或第三方异常。"""


def execution_contract() -> dict[str, object]:
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
        raise RegexSearchError(
            "tool_regex_invalid",
            "表达式不是受支持的RE2模式或超过编译内存上限；请修正语法、移除反向引用/"
            "前后查找断言、简化表达式，或显式使用match_mode=literal查找字面量",
        ) from None
    operation.checkpoint()

    def first_match_index(line: str) -> int:
        match = compiled.search(line)
        return -1 if match is None else int(match.start())

    return first_match_index
