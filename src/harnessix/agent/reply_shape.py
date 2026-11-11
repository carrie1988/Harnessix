"""无工具调用回复的形态判定：只看正文，区分答复与几类不是答复的文本。

职责：去掉模型误写的线上文本外壳；把正文归为答复、空回复、宿主记录、写成文本的工具调用或没写完的
结构化片段；判断正文是否宣布还要继续。供完成守卫（completion_guard）决定是否补发提示。

非职责：不读Turn状态，不决定是否补发，也不解析、修复或执行正文里的工具调用。宿主只执行Provider
工具调用通道交来的调用：正文可能只是引用了工具输出或文件内容，且没有调用身份、目录校验与审批。
"""

from __future__ import annotations

import json
import re
from typing import Literal

ReplyShape = Literal["answer", "empty", "host_record", "call_as_text", "cut_off"]

# 宿主写进模型历史的记录：上下文摘要（v1 JSON的schema、v2说明头）与被替换回复的占位。照抄不是答复。
HOST_RECORD_MARKERS = (
    "harnessix.compaction-summary",
    "[Host summary of earlier history",
    "[Harnessix host placeholder",
)

# 模型手写的JSON常在字符串里带原始换行；宽松解码只放行控制字符，结构仍须完整。
_DECODER = json.JSONDecoder(strict=False)
_FENCE_OPENING = re.compile(r"```[\w-]*[ \t]*\n")
# 以带引号的键开头的对象（可在数组内）是JSON记录或工具调用的开头，自然语言不这样起头。
_RECORD_OPENING = re.compile(r"""\[?\s*\{\s*["'](?:[^"'\\\n]|\\.)*["']\s*:""")
# 对话模板的工具调用标记出现在行首，说明模型把调用写进了正文；行内提及（如反引号里）不算。
_CALL_MARKUP = re.compile(
    r"^[ \t]*(?:<\|?(?:tool_calls?|function_calls?|tool_use|invoke\b|function=)|\[TOOL_CALLS\])",
    re.MULTILINE | re.IGNORECASE,
)
_CALL_BLOCK_TYPES = ("tool_use", "tool_call", "function_call", "function")
_CALL_ARGUMENT_KEYS = ("arguments", "parameters", "input")
# 外壳套外壳的层数上限；超过后按最后一层的原文判定。
_MAX_ENVELOPE_DEPTH = 3

# 宣布下一步的开头（英文与中文，小写比较）。
_CONTINUATION_OPENINGS = (
    "next step",
    "next,",
    "next i",
    "let me",
    "let's",
    "i'll",
    "i will",
    "i need to",
    "now i",
    "now let",
    "下一步",
    "接下来",
    "让我",
    "我将",
    "我先",
    "我需要",
    "现在我",
)
# 结尾句里的第一人称继续意图。结尾句常是给用户的建议或客套（“Let me know…”“Next steps: …”），
# 所以只认说话人自己要做的事，不认泛指的“next step”。
_CLOSING_INTENT = re.compile(
    r"\b(?:my next step|next,? i\b|let me(?! know)|let's|i'll|i will|i(?: a|')m going to"
    r"|i (?:(?:just|still|now|first|also|only) )*need\b)"
    r"|让我|我将|我先|我需要|接下来我|下一步我",
    re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def reply_text(reply: str) -> str:
    """去掉线上文本外壳后的正文；外壳里带着工具调用、或回复不是外壳时原样返回。

    模型偶尔把答复包成内容块JSON（{"type":"text","text":…}）或整条消息外壳。壳里只有文本时没有
    任何动作被漏掉，判定应针对壳内正文；持久化的回复不变。
    """
    text = reply
    for _ in range(_MAX_ENVELOPE_DEPTH):
        decoded = _leading_json(_unfenced(text.strip()))
        if decoded is None or decoded[1]:
            return text
        parts = _wire_parts(decoded[0])
        if parts is None or parts[1]:
            return text
        text = parts[0]
    return text


def reply_shape(text: str) -> ReplyShape:
    """正文的形态；text应先经reply_text去壳。

    - empty：只有空白；
    - host_record：照抄宿主记录，或整条回复是一个JSON对象；
    - call_as_text：整条回复是带工具调用的线上外壳，或行首调用标记后跟着完整JSON；
    - cut_off：以JSON记录开头却无法解析，或调用标记后没有完整JSON（被截断或写坏）；
    - answer：其余，包括以JSON开头、后面还有说明文字的正文。
    """
    stripped = text.strip()
    if not stripped:
        return "empty"
    if any(marker in stripped for marker in HOST_RECORD_MARKERS):
        return "host_record"
    markup = _CALL_MARKUP.search(stripped)
    if markup is not None:
        return _marked_up_call_shape(stripped[markup.end() :])
    body = _unfenced(stripped)
    if not body.startswith(("{", "[")):
        return "answer"
    decoded = _leading_json(body)
    if decoded is None:
        return "cut_off" if _RECORD_OPENING.match(body) else "answer"
    value, rest = decoded
    if rest:
        return "answer"
    parts = _wire_parts(value)
    if parts is not None and parts[1]:
        return "call_as_text"
    return "host_record" if isinstance(value, dict) else "answer"


def announces_next_step(text: str) -> bool:
    """正文宣布还要继续：开头或结尾句说了自己的下一步，或以冒号结尾；后面本应跟着工具调用。"""
    stripped = text.strip().replace("’", "'")
    # 去掉行首Markdown标记（如“**Next step:**”）后再比较开头。
    opening = stripped.lstrip("*#>-_` ").lower()
    return (
        opening.startswith(_CONTINUATION_OPENINGS)
        or stripped.endswith((":", "："))
        or _CLOSING_INTENT.search(_closing_sentence(stripped)) is not None
    )


def _closing_sentence(text: str) -> str:
    sentences = [part for part in _SENTENCE_BREAK.split(text) if part.strip()]
    return sentences[-1] if sentences else ""


def _unfenced(stripped: str) -> str:
    """去掉包住整段正文的Markdown代码围栏；没有围栏时原样返回，缺少收尾围栏说明正文被截断。"""
    opening = _FENCE_OPENING.match(stripped)
    if opening is None:
        return stripped
    return stripped[opening.end() :].removesuffix("```").strip()


def _leading_json(text: str) -> tuple[object, str] | None:
    """正文开头的完整JSON对象或数组，及其后的剩余文本；开头不是完整JSON容器时为None。"""
    if not text.startswith(("{", "[")):
        return None
    try:
        value, end = _DECODER.raw_decode(text)
    except (ValueError, RecursionError):
        return None
    return value, text[end:].strip()


def _marked_up_call_shape(after_markup: str) -> ReplyShape:
    """调用标记之后跟着完整JSON对象，即是写成文本的调用；否则调用没有写完或写坏了。"""
    start = after_markup.find("{")
    complete = start != -1 and _leading_json(after_markup[start:]) is not None
    return "call_as_text" if complete else "cut_off"


def _wire_parts(value: object) -> tuple[str, bool] | None:
    """把线上外壳拆成（壳内文本，是否含工具调用）；value不是内容块、内容块数组或消息外壳时为None。"""
    blocks = _content_blocks(value)
    if blocks is None:
        return None
    texts: list[str] = []
    has_call = False
    for block in blocks:
        if _is_call(block):
            has_call = True
            continue
        text = _block_text(block)
        if text is None:
            return None
        texts.append(text)
    return "".join(texts), has_call


def _content_blocks(value: object) -> list[dict[str, object]] | None:
    """单个内容块、内容块数组，或消息外壳content数组里的内容块；其他形状为None。"""
    if isinstance(value, dict):
        content = value.get("content")
        if not isinstance(content, list):
            return [value]
        value = content
    if isinstance(value, list) and value and all(isinstance(entry, dict) for entry in value):
        return value
    return None


def _is_call(block: dict[str, object]) -> bool:
    """工具调用的线上形状：调用类型标记、name加参数、function对象或非空tool_calls数组。"""
    function = block.get("function")
    return (
        block.get("type") in _CALL_BLOCK_TYPES
        or (isinstance(block.get("name"), str) and any(k in block for k in _CALL_ARGUMENT_KEYS))
        or (isinstance(function, dict) and isinstance(function.get("name"), str))
        or (isinstance(block.get("tool_calls"), list) and bool(block["tool_calls"]))
    )


def _block_text(block: dict[str, object]) -> str | None:
    """文本内容块的text，或只带字符串正文的助手消息的content；其他块为None。"""
    if block.get("type") == "text":
        text = block.get("text")
    elif block.get("role") == "assistant":
        text = block.get("content")
    else:
        text = None
    return text if isinstance(text, str) else None
