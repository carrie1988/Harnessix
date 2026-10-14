"""仅用有界tokenize观察Python声明范围，不执行代码或声称AST语义。"""

from __future__ import annotations

import hashlib
import io
import tokenize
from collections.abc import Callable, Iterator
from typing import Literal

from harnessix.tools.contracts import (
    MAX_LINE_BYTES,
    MAX_SCAN_BYTES,
    DefinitionHeader,
    DefinitionView,
    ReadFileInputV4,
    ReadFileViewOutput,
    ReadToolError,
    SourceLineRange,
)
from harnessix.tools.workspace import ReadOperation

MAX_SOURCE_LINES = 100_000
MAX_HEADER_BYTES = 4096
_TRIVIA = {tokenize.COMMENT, tokenize.NL}


def source_lines(
    content: bytes, operation: ReadOperation, decode: Callable[[bytes], str]
) -> list[str]:
    """验证完整观察的每一物理行；CRLF与BOM保留在返回原文中。"""

    if len(content) > MAX_SCAN_BYTES:
        raise ReadToolError("limit_exceeded")
    lines: list[str] = []
    with io.BytesIO(content) as stream:
        while True:
            operation.checkpoint()
            raw = stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return lines
            if len(raw) > MAX_LINE_BYTES or len(lines) >= MAX_SOURCE_LINES:
                raise ReadToolError("limit_exceeded")
            lines.append(decode(raw))


def _tokens(lines: list[str], operation: ReadOperation) -> Iterator[tokenize.TokenInfo]:
    row = 0

    def readline() -> str:
        nonlocal row
        operation.checkpoint()
        if row >= len(lines):
            return ""
        line = lines[row]
        row += 1
        # UTF-8 BOM只对词法器不可见；原始摘要、header和正文都不重写。
        return line.removeprefix("\ufeff") if row == 1 else line

    try:
        for token in tokenize.generate_tokens(readline):
            operation.checkpoint()
            if token.type == tokenize.ERRORTOKEN:
                raise ReadToolError("definition_unavailable")
            yield token
    except (tokenize.TokenError, SyntaxError):
        raise ReadToolError("definition_unavailable") from None


def _declaration_kind(
    first: tokenize.TokenInfo, tokens: Iterator[tokenize.TokenInfo]
) -> Literal["function", "class"]:
    keyword = first.string
    if keyword == "async":
        following = next(tokens)
        if following.type != tokenize.NAME or following.string != "def":
            raise ReadToolError("definition_unavailable")
        keyword = "def"
    if first.type != tokenize.NAME or keyword not in {"def", "class"}:
        raise ReadToolError("definition_unavailable")
    name = next(tokens)
    if name.type != tokenize.NAME or not name.string.isidentifier():
        raise ReadToolError("definition_unavailable")
    return "function" if keyword == "def" else "class"


def _header(
    first: tokenize.TokenInfo, tokens: Iterator[tokenize.TokenInfo]
) -> tuple[Literal["function", "class"], int]:
    kind = _declaration_kind(first, tokens)
    brackets: list[str] = []
    has_parameters = False
    for token in tokens:
        if token.type in {tokenize.NEWLINE, tokenize.ENDMARKER}:
            break
        if token.type != tokenize.OP:
            continue
        if token.string in {"(", "[", "{"}:
            has_parameters |= token.string == "(" and not brackets
            brackets.append(token.string)
        elif token.string in {")", "]", "}"}:
            if not brackets or brackets.pop() != {")": "(", "]": "[", "}": "{"}[token.string]:
                break
        elif token.string == ":" and not brackets:
            if kind == "function" and not has_parameters:
                break
            return kind, token.start[0]
    raise ReadToolError("definition_unavailable")


def _plain_string(token: tokenize.TokenInfo) -> bool:
    if token.type != tokenize.STRING:
        return False
    prefix = token.string.split("'", 1)[0].split('"', 1)[0].lower()
    return prefix in {"", "r", "u"}


def _inline_body_end(tokens: Iterator[tokenize.TokenInfo]) -> int | None:
    inline = False
    for token in tokens:
        if token.type in _TRIVIA:
            continue
        if token.type == tokenize.NEWLINE:
            return token.start[0] if inline else None
        if token.type == tokenize.ENDMARKER:
            raise ReadToolError("definition_unavailable")
        inline = True
    raise ReadToolError("definition_unavailable")


def _require_indent(tokens: Iterator[tokenize.TokenInfo]) -> None:
    for token in tokens:
        if token.type in _TRIVIA:
            continue
        if token.type != tokenize.INDENT:
            raise ReadToolError("definition_unavailable")
        return
    raise ReadToolError("definition_unavailable")


def _leading_docstring(
    tokens: Iterator[tokenize.TokenInfo], header_end: int
) -> tuple[int, int] | None:
    """仅省略紧邻声明、无附带注释的独立文本literal，其他形式保守保留。"""

    count = 0
    has_comment = False
    literal: tokenize.TokenInfo | None = None
    for token in tokens:
        if token.type == tokenize.NEWLINE:
            break
        if token.type == tokenize.COMMENT:
            has_comment = True
        elif token.type != tokenize.NL:
            count += 1
            literal = token if count == 1 and _plain_string(token) else None
    if (
        count == 1
        and literal is not None
        and literal.start[0] == header_end + 1
        and not has_comment
    ):
        return literal.start[0], literal.end[0]
    return None


def _dedent_body_end(tokens: Iterator[tokenize.TokenInfo]) -> int:
    depth = 1
    for token in tokens:
        if token.type == tokenize.INDENT:
            depth += 1
        elif token.type == tokenize.DEDENT:
            depth -= 1
            if not depth:
                # tokenize把尾随空行/注释放在DEDENT之前，正文保持连续原行。
                return token.start[0] - 1
    raise ReadToolError("definition_unavailable")


def _body(
    tokens: Iterator[tokenize.TokenInfo], header_end: int
) -> tuple[int, int, tuple[int, int] | None]:
    inline_end = _inline_body_end(tokens)
    if inline_end is not None:
        return header_end, inline_end, None
    _require_indent(tokens)
    omitted = _leading_docstring(tokens, header_end)
    start = omitted[1] + 1 if omitted else header_end + 1
    return start, _dedent_body_end(tokens), omitted


def definition_span(
    lines: list[str], anchor_line: int, operation: ReadOperation
) -> tuple[Literal["function", "class"], int, int, int, tuple[int, int] | None]:
    """只接受逻辑语句起始的声明；完整消费词法流，后方损坏也不能发布。"""

    tokens = _tokens(lines, operation)
    statement_start = True
    for token in tokens:
        if token.type in _TRIVIA | {tokenize.INDENT, tokenize.DEDENT}:
            continue
        if token.type == tokenize.NEWLINE:
            statement_start = True
            continue
        if statement_start and token.start[0] == anchor_line:
            kind, header_end = _header(token, tokens)
            if sum(len(line.encode("utf-8")) for line in lines[anchor_line - 1 : header_end]) > (
                MAX_HEADER_BYTES
            ):
                raise ReadToolError("limit_exceeded")
            start, end, omitted = _body(tokens, header_end)
            for _token in tokens:
                pass
            return kind, header_end, start, end, omitted
        statement_start = False
    raise ReadToolError("definition_unavailable")


def definition_page(
    content: bytes,
    args: ReadFileInputV4,
    anchor_line: int,
    revision: str,
    operation: ReadOperation,
    decode: Callable[[bytes], str],
) -> ReadFileViewOutput:
    """从唯一原始字节观察投影连续实现页；截断仅表示实现分页。"""

    lines = source_lines(content, operation, decode)
    kind, header_end, start, end, omitted = definition_span(lines, anchor_line, operation)
    page: list[str] = []
    returned = 0
    reason: Literal["line_limit", "byte_limit"] | None = None
    for number in range(start, end + 1):
        operation.checkpoint()
        if len(page) >= args.max_lines:
            reason = "line_limit"
            break
        line = lines[number - 1]
        size = len(line.encode("utf-8"))
        if returned + size > args.max_utf8_bytes:
            reason = "byte_limit"
            break
        returned += size
        page.append(line)
    operation.checkpoint()
    raw_sha = hashlib.sha256(content).hexdigest()
    operation.checkpoint()
    return ReadFileViewOutput(
        path=args.path,
        text="".join(page),
        start_line=start,
        end_line=start + len(page) - 1 if page else None,
        utf8_bytes=returned,
        revision=revision,
        truncated=reason is not None,
        truncation_reason=reason,
        next_line=start + len(page) if reason else None,
        file_bytes=len(content),
        content_sha256=raw_sha,
        digest_status="complete",
        definition=DefinitionView(
            kind=kind,
            header=DefinitionHeader(
                start_line=anchor_line,
                end_line=header_end,
                text="".join(lines[anchor_line - 1 : header_end]),
            ),
            body_start_line=start,
            body_end_line=end,
            omitted_docstring=(
                SourceLineRange(start_line=omitted[0], end_line=omitted[1]) if omitted else None
            ),
        ),
    )
