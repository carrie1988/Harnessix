"""读工具写给模型的失败说明：每种错误码一句话说明发生了什么与下一步（从tools.runtime拆出）。"""

from __future__ import annotations

from typing import Final

# 写给模型的失败说明（英文）：说明发生了什么与下一步，不回显路径或查询值；
# 界面按错误码另行展示用户文案（product_ui.tool_summary）。
READ_ERROR_MESSAGES: Final = {
    "anchor_not_found": (
        "No line of the file contains anchor. anchor is literal text inside one line (not a "
        "regex, no line breaks); copy it from a line you have seen in this file, or locate the "
        "line with grep on this path and read by start_line"
    ),
    "anchor_ambiguous": (
        "More than one line contains anchor. Use a longer piece of the line that occurs once, "
        "or grep this path for it and read by start_line"
    ),
    "anchor_outside_page": (
        "The page ended before the anchor line. Use a smaller lines_before or a larger max_lines"
    ),
    "definition_unavailable": (
        "view=definition works only for a real def/class header in a Python file within the "
        "size limit. Use view=text, or check that anchor is the def/class line"
    ),
    "limit_exceeded": (
        "The scan hit a tool limit (entries, bytes or depth) and returned nothing. Do not "
        "retry unchanged: set path to a more specific subdirectory, or find candidate files "
        "with glob first and grep those"
    ),
    # 只说明可操作的下一步，不区分“越界”与“敏感路径拒绝”，不回显路径。
    "path_denied": (
        "This path cannot be read. Read tools only read files inside the workspace, addressed "
        "relative to the workspace root (for example src/app.py). Use the command tool for "
        "paths outside it such as temporary directories; some sensitive paths are never readable"
    ),
    "not_found": (
        "Path not found. Paths are relative to the workspace root; confirm the path with glob "
        "or list_files before retrying"
    ),
    "wrong_file_type": (
        "The path is a directory where a file is needed, or a file where a directory is "
        "needed. Use list_files or glob for directories and read_file for files"
    ),
    "invalid_utf8": "The file is not valid UTF-8 text. Inspect it with the command tool",
    "binary_file": "The file contains binary data. Inspect it with the command tool",
    "offset_out_of_range": (
        "The start position is past the end. Read from line 1, or use the next_line or "
        "next_offset value of the previous page"
    ),
    "page_changed": (
        "The file or directory changed since expected_revision was issued. Read again without "
        "expected_revision"
    ),
    "workspace_changed": "The workspace changed during the read. Retry the call",
    "no_files_searched": (
        "No files were searched: no file under path matches include. include filters by file "
        "name (for example *.py) or by a path relative to path; to search one known file pass "
        "it as path, and check names with glob"
    ),
    "io_failed": "Reading failed with an I/O error. Retry, or use the command tool",
    "timeout": (
        "The read or search timed out and returned nothing. Do not retry unchanged: set path "
        "to a specific subdirectory, narrow include, or find candidate files with glob first"
    ),
}

READ_ERROR_FALLBACK: Final = "工作区读取未完成"

# Glob超出扫描上限时的下一步：此前与grep共用“先用glob找候选文件”，
# 而失败的正是glob（Beta Issue #35）。
GLOB_LIMIT_HINT: Final = (
    "The scan hit a tool limit (entries, bytes or depth) and returned nothing. Do not retry "
    "unchanged: set path to a more specific subdirectory, or replace leading * or ** segments "
    "of the pattern with the directory names you expect"
)
