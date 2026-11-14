"""有界搜索的执行合同：Glob与各类Grep的版本、上限与超时行为（从search拆出，内容不变）。"""

from __future__ import annotations

from harnessix.tools.grep_matcher import execution_contract as regex_execution_contract
from harnessix.tools.search_contracts import (
    IGNORED_DIRECTORIES,
    MAX_GREP_CONTEXT_LINES,
    MAX_MATCH_BYTES,
    MAX_SEARCH_DEPTH,
    MAX_SEARCH_ENTRIES,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_NAMES_BYTES,
    MAX_SEARCH_RECORD_BYTES,
    MAX_SEARCH_TOTAL_BYTES,
    MAX_SNAPSHOT_GREP_CONTEXT_LINES,
    MAX_SNAPSHOT_SEARCH_ENTRIES,
    MAX_SNAPSHOT_SEARCH_NAMES_BYTES,
    MAX_SNAPSHOT_SEARCH_RECORD_BYTES,
    MAX_SNAPSHOT_SEARCH_TOTAL_BYTES,
)


def execution_contract(
    *, enable_regex_search: bool = False, snapshot_grep: bool = False
) -> dict[str, object]:
    """原字面量Grep与Glob合同不变；快照与正则Grep另有整仓库上限、文件名include与更宽上下文。

    v7（快照）与v8（正则）：整仓库上限放宽到四倍；整份文本不命中的文件不再逐行扫描，
    超长行只在可能命中的文件里计数（ADR 0145）。此前分别是v5、v6。
    v9（快照）与v10（正则）：读取时限到了而已有命中时返回这些命中，标为time_limit（ADR 0162）。
    """
    rules: dict[str, object] = {
        "version": "bounded-search/v3",
        "start_path": "regular-file-or-directory",
        "pattern": "case-sensitive-segments-and-globstar",
        "grep": "case-sensitive-literal-lines",
        "ignored_directories": IGNORED_DIRECTORIES,
        "max_entries": MAX_SEARCH_ENTRIES,
        "max_names_bytes": MAX_SEARCH_NAMES_BYTES,
        "max_depth": MAX_SEARCH_DEPTH,
        "max_file_bytes": MAX_SEARCH_FILE_BYTES,
        "max_total_bytes": MAX_SEARCH_TOTAL_BYTES,
        "max_record_bytes": MAX_SEARCH_RECORD_BYTES,
        "max_match_bytes": MAX_MATCH_BYTES,
        "max_grep_context_lines": MAX_GREP_CONTEXT_LINES,
    }
    if snapshot_grep or enable_regex_search:
        rules.update(
            version="bounded-search/v9",
            on_timeout="return-matches-found-so-far-as-time_limit",
            snapshot_grep={
                "include": "basename-at-any-depth-without-slash-else-segments-and-globstar",
                "max_entries": MAX_SNAPSHOT_SEARCH_ENTRIES,
                "max_names_bytes": MAX_SNAPSHOT_SEARCH_NAMES_BYTES,
                "max_total_bytes": MAX_SNAPSHOT_SEARCH_TOTAL_BYTES,
                "max_record_bytes": MAX_SNAPSHOT_SEARCH_RECORD_BYTES,
                "max_context_lines": MAX_SNAPSHOT_GREP_CONTEXT_LINES,
            },
        )
    if enable_regex_search:
        rules.update(
            version="bounded-search/v10",
            grep="case-sensitive-re2-or-explicit-literal-lines",
            regex=regex_execution_contract(),
        )
    return rules
