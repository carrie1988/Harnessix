"""共享逐行匹配核心；RE2缺失或漂移时失败关闭，不回退到回溯引擎。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib import metadata

from harnessix.agent.errors import KernelError
from harnessix.tools.search_contracts import GrepInput, RegexGrepInput
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
        "semantics": "RE2.search-first-match-per-line-no-pattern-rewriting",
        "text": "UTF-8-no-normalization-CRLF-stripped-for-matching-only",
        "unsupported": "backreferences-and-lookaround-rejected",
        "compile": "once-per-call-before-file-scan",
        # 官方compile的LRU最多128项；每项RE2预算1 MiB，合计最多128 MiB。
        # 仅使用公开API，不绕过或清理该进程共享缓存。
        "engine_cache_entries": 128,
        "engine_cache_max_mem": 128 * RE2_MAX_MEM,
    }


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
