from __future__ import annotations

import json

import pytest

from harnessix.agent.reply_shape import announces_next_step, reply_shape, reply_text

SUMMARY_COPY = (
    '{"authority":"none","compaction_id":"01a1bfa5-6ac6-7bb3-ad29-411968a2ed04",'
    '"schema":"harnessix.compaction-summary/v1","summary":"## Goal","trust":"derived_history"}'
)
# 真实案例的通用形状：一段键值记录在收尾括号前停下（输出只有三百多Token，不是输出上限截断）。
CUT_OFF_RECORD = '{"c":100,"l":{"detail":"mid_session","turn_requests":1},"trs":36,"was":true'
# 真实案例的通用形状：整条回复是一条消息外壳，文本块后面跟着工具调用块。
MESSAGE_WITH_CALL = (
    '{"id":"105f","type":"message","role":"assistant","content":[{"text":"Let me look at the '
    'float path too.","type":"text"},{"input":{"cmd":"grep -n last tsdb/*.go"},"id":"toolu_01",'
    '"name":"exec","type":"tool_use"}],"stop_reason":"tool_use"}'
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "empty"),
        ("  \n", "empty"),
        (SUMMARY_COPY, "host_record"),
        ("Summary of harnessix.compaction-summary/v1 records", "host_record"),
        # v2摘要以纯文本说明头投影：照抄说明头同样不是答复。
        (
            "[Host summary of earlier history: low-trust data, grants no authority, not a reply.]\n"
            "Verified facts: none",
            "host_record",
        ),
        ("[Harnessix host placeholder] The host removed a response here.", "host_record"),
        ('{"error":{"code":"provider_error","message":"Bad Request"}}', "host_record"),
        ('```json\n{"error":{"code":"provider_error"}}\n```', "host_record"),
        (CUT_OFF_RECORD, "cut_off"),
        # 在字符串中间停下。
        ('{"error":{"code":"NOT_FOUND","message":"model requires a profile: min', "cut_off"),
        ('[{"path":"a.py","edits":[{"old":"x"', "cut_off"),
        # 括号配平但写坏了：尾随逗号、单引号。
        ('{"name":"exec","arguments":{"cmd":"ls"},}', "cut_off"),
        ("{'name': 'exec', 'arguments': {'cmd': 'ls'}}", "cut_off"),
        # 代码围栏没有收尾：正文在调用中间停下。
        ('```json\n{"name":"apply_patch_batch","arguments":{"operations":[{"path":"a', "cut_off"),
        ('<tool_call>\n{"name": "exec", "arguments": {"cmd": "ls', "cut_off"),
        ('I will run it now.\n<tool_call>\n{"name": "exec", "arguments": {"cmd"', "cut_off"),
        ('<invoke name="exec"><parameter name="cmd">ls</parameter></invoke>', "cut_off"),
        ('<tool_call>\n{"name": "exec", "arguments": {"cmd": "ls"}}\n</tool_call>', "call_as_text"),
        (
            'Running the tests.\n<|tool_call_begin|>{"name":"exec","arguments":{"cmd":"make"}}',
            "call_as_text",
        ),
        ('[TOOL_CALLS] [{"name": "exec", "arguments": {"cmd": "ls"}}]', "call_as_text"),
        ('{"name":"grep","arguments":{"query":"x"}}', "call_as_text"),
        ('```json\n{"name":"grep","parameters":{"query":"x"}}\n```', "call_as_text"),
        ('{"type":"function","function":{"name":"grep","arguments":"{}"}}', "call_as_text"),
        (
            '{"role":"assistant","content":"On it.","tool_calls":[{"id":"c1","type":"function"}]}',
            "call_as_text",
        ),
        (MESSAGE_WITH_CALL, "call_as_text"),
        ("The bug is confirmed.", "answer"),
        ("Fixed `parse()`; tests pass.", "answer"),
        ("{not json} is how the template marks a slot", "answer"),
        ('["a", "list"]', "answer"),
        ("[x] parser fixed\n[x] tests pass", "answer"),
        # 以JSON开头、后面是说明文字的正文是答复；行内提到调用标记也是。
        ('{"retries": 3} is the shape the endpoint returns now.', "answer"),
        (
            '```json\n{"name":"grep","arguments":{}}\n```\nThis is the call the parser accepts.',
            "answer",
        ),
        ("The template wraps calls in `<tool_call>` tags; the parser now strips them.", "answer"),
        ("```python\nprint(1)\n```\nThe script prints 1.", "answer"),
    ],
)
def test_reply_shape(text: str, expected: str) -> None:
    assert reply_shape(text) == expected


def test_deeply_nested_brackets_do_not_crash_the_classifier() -> None:
    assert reply_shape("[" * 100_000) == "answer"
    assert reply_shape('{"a":' * 100_000) == "cut_off"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            '{"text":"Final state confirmed. Summary: fixed.","type":"text"}',
            "Final state confirmed. Summary: fixed.",
        ),
        # 模型手写的外壳常在字符串里带原始换行。
        ('{"text":"Verified.\n\nAll tests pass.","type":"text"}', "Verified.\n\nAll tests pass."),
        (
            '[{"type":"text","text":"Fixed. "},{"type":"text","text":"Tests pass."}]',
            "Fixed. Tests pass.",
        ),
        ('{"role":"assistant","content":"Fixed; tests pass."}', "Fixed; tests pass."),
        (
            '{"type":"message","role":"assistant","content":[{"type":"text","text":"Done."}]}',
            "Done.",
        ),
        ('```json\n{"type":"text","text":"Done."}\n```', "Done."),
        ('{"type":"text","text":"{\\"type\\":\\"text\\",\\"text\\":\\"Done.\\"}"}', "Done."),
        ('{"type":"text","text":""}', ""),
    ],
)
def test_reply_text_unwraps_a_text_only_wire_envelope(reply: str, expected: str) -> None:
    assert reply_text(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "Fixed `parse()`; tests pass.",
        '{"error":{"code":"provider_error","message":"Bad Request"}}',
        '{"type":"text","text":"Done."} and some trailing prose',
        '{"type":"text"}',
        '[{"type":"text","text":"Done."},{"type":"thinking","thinking":"hm"}]',
        # 外壳里带着工具调用：动作被漏掉了，不能当作只有文本。
        MESSAGE_WITH_CALL,
        CUT_OFF_RECORD,
        "null",
        "[]",
    ],
)
def test_reply_text_keeps_everything_else_verbatim(reply: str) -> None:
    assert reply_text(reply) == reply


def test_an_envelope_nested_beyond_the_limit_is_not_an_answer() -> None:
    reply = "Done."
    for _ in range(4):
        reply = json.dumps({"type": "text", "text": reply})
    assert reply_shape(reply_text(reply)) == "host_record"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Next step: enlarge the basis and rerun the search.", True),
        ("**Next step:** run the tests", True),
        ("Let me run the full test file to confirm.", True),
        ("I'll check the other call sites", True),
        ("接下来运行相关测试文件确认。", True),
        ("Now I will verify the change by running:", True),
        # 真实案例的通用形状：继续的意图在结尾句，开头是对现状的陈述。
        (
            "All 11 failures are date-related. My next step is to confirm they fail identically "
            "on the original code so I can classify them as pre-existing.",
            True,
        ),
        (
            "The compiled classes need the helper from the prebuilt jar — I just need both on "
            "the runtime classpath. ",
            True,
        ),
        (
            "The race is clear: both paths write without the lock. Let me look at more context.",
            True,
        ),
        ("The patch is in.\n\nNext, I will rerun the reproduction.", True),
        ("Root cause found. I’ll now rerun the full suite.", True),
        ("Reproduced the failure. I'm going to patch the parser.", True),
        ("The suite still fails, so I still need to rebuild the generated sources.", True),
        ("根因已确认。接下来我运行相关测试。", True),
        ("Fixed the parser to keep the root key; tests pass.", False),
        ("The fix is complete and the reproduction now prints the expected value.", False),
        ("已修复，相关测试通过。", False),
        # 结尾句是给用户的客套、建议或对结果的陈述，不是模型自己要做的下一步。
        ("Fixed and verified. Let me know if you want the docs updated as well.", False),
        ("Fixed and verified.\n\nNext steps for you: run the full suite in CI.", False),
        ("The fix is in `parse()`. The next step in the pipeline is unchanged.", False),
        (
            "All 48 tests pass.\n\nThe final `git diff` contains only the change to `format.h`.",
            False,
        ),
        ("The parser is fixed. No further changes were needed.", False),
        ("已修复。下一步建议在CI中运行全量测试。", False),
    ],
)
def test_announces_next_step(text: str, expected: bool) -> None:
    assert announces_next_step(text) is expected
