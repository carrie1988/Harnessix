"""CRLF与混合换行文件上的edit走真实产品路径：读取、多行LF锚点、后续单行编辑、原样抄回的锚点。"""

from __future__ import annotations

import hashlib

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from tests.agent.helpers import answer
from tests.product_config.test_product_patch_rollback import approve, product

# CRLF区在后、LF区在前的混合换行文件；模型读到的文本带着\r\n，发回的old_text与new_text却是LF。
LF_REGION = b"import os\n\n\ndef limit():\n    return 1\n"
CRLF_REGION = (
    b"\r\n\r\nasync def read_upload(file):\r\n    data = await file.read()\r\n"
    b"    if not data:\r\n        raise ValueError(file)\r\n    return data\r\n"
)
MIXED = LF_REGION + CRLF_REGION


def tool_step(tool, call_id, **arguments):
    return [
        ResponseStarted(response_id=f"{call_id}-response"),
        ToolCallCompleted(call_id=call_id, tool=tool, arguments=arguments),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def tool_results(turn):
    return [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]


def mixed_edit(sha, old_text, new_text):
    return {
        "operation": "edit",
        "path": "src/service.py",
        "expected_sha256": sha,
        "edits": [{"old_text": old_text, "new_text": new_text}],
    }


async def patch_and_approve(runtime, provider, thread_id, request_id, file_entry):
    """提交一次补丁并批准；返回补丁的工具结果。"""
    provider.steps = (
        tool_step("apply_patch_batch", request_id, files=[file_entry]),
        answer("已处理"),
    )
    waiting = await runtime.run_turn(thread_id, "修改文件", request_id=request_id)
    assert waiting.status is TurnStatus.WAITING_APPROVAL, tool_results(waiting)[-1].error
    completed = await approve(runtime, thread_id, waiting)
    patched = tool_results(completed)[-1]
    assert patched.outcome == "succeeded"
    return patched


async def test_edits_on_a_crlf_region_match_and_keep_its_line_endings(tmp_path):
    # 回归（Beta RT-06，state-ocr事件594–726）：混合换行文件的CRLF区上，多行LF锚点连续5次
    # “锚点不存在”；随后单行锚点命中，LF的new_text被原样写入，CRLF区里出现了LF行。
    async with product(tmp_path) as (root, runtime, provider, *_):
        target = root / "src/service.py"
        target.write_bytes(MIXED)
        read_sha = hashlib.sha256(MIXED).hexdigest()
        provider.steps = (tool_step("read_file", "read", path="src/service.py"), answer("已读取"))
        thread = await runtime.create_thread(str(root))
        read_turn = await runtime.run_turn(thread.thread_id, "读取文件", request_id="read")

        # 模型读到的正文带着原始的\r\n；前置条件按文件的真实字节核对。
        [read] = tool_results(read_turn)
        assert "await file.read()\r\n" in read.output["text"]
        assert read.output["content_sha256"] == read_sha

        multi_line = mixed_edit(
            read_sha,
            "    data = await file.read()\n    if not data:\n",
            "    data = await file.read(limit())\n    if not data:\n",
        )
        patched = await patch_and_approve(runtime, provider, thread.thread_id, "first", multi_line)
        edited = MIXED.replace(b"file.read()\r\n", b"file.read(limit())\r\n")
        assert target.read_bytes() == edited
        # 结果里的摘要是写入后的真实字节，可直接用作下一次编辑的前置条件。
        edited_sha = hashlib.sha256(edited).hexdigest()
        assert patched.output["changes"] == [
            {"path": "src/service.py", "content_sha256": edited_sha}
        ]

        # 后续的单行锚点逐字节命中，new_text是多行LF：写入仍是CRLF。
        single_line = mixed_edit(
            edited_sha, "    return data", "    size = len(data)\n    return data"
        )
        await patch_and_approve(runtime, provider, thread.thread_id, "second", single_line)
        final = edited.replace(
            b"    return data\r\n", b"    size = len(data)\r\n    return data\r\n"
        )
        assert target.read_bytes() == final

        # 模型也会把读到的\r\n原样抄回：逐字节命中，写入的同样是CRLF。
        copied = mixed_edit(
            hashlib.sha256(final).hexdigest(),
            "    if not data:\r\n        raise ValueError(file)\r\n",
            "    if not data:\r\n        raise ValueError(data)\r\n",
        )
        await patch_and_approve(runtime, provider, thread.thread_id, "third", copied)
        final = final.replace(b"ValueError(file)\r\n", b"ValueError(data)\r\n")
        assert target.read_bytes() == final
        # LF区一个字节未动；CRLF区里没有落单的LF。
        assert final.startswith(LF_REGION) and b"\r" not in LF_REGION
        assert final[len(LF_REGION) :].count(b"\n") == final[len(LF_REGION) :].count(b"\r\n")
