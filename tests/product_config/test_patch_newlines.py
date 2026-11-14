"""CRLF与混合换行文件上的edit走真实产品路径：读取、多行LF锚点、后续单行编辑与前置条件补全。"""

from __future__ import annotations

import hashlib

from harnessix.agent.models import ToolResultContent, TurnStatus
from tests.agent.helpers import answer
from tests.product_config.test_patch_call_repair import STALE, patch_result, run_patch, tool_step
from tests.product_config.test_product_patch_rollback import approve, product

# CRLF区在后、LF区在前的混合换行文件；模型读到的文本带着\r\n，发回的old_text与new_text却是LF。
LF_REGION = b"import os\n\n\ndef limit():\n    return 1\n"
CRLF_REGION = (
    b"\r\n\r\nasync def read_upload(file):\r\n    data = await file.read()\r\n"
    b"    if not data:\r\n        raise ValueError(file)\r\n    return data\r\n"
)
MIXED = LF_REGION + CRLF_REGION


def mixed_edit(sha, old_text, new_text):
    return {
        "operation": "edit",
        "path": "src/service.py",
        "expected_sha256": sha,
        "edits": [{"old_text": old_text, "new_text": new_text}],
    }


async def test_edits_on_a_crlf_region_match_and_keep_its_line_endings(tmp_path):
    # 回归（Beta RT-06，state-ocr事件594–726）：混合换行文件的CRLF区上，多行LF锚点连续5次
    # “未找到”；随后单行锚点命中，LF的new_text被原样写入，CRLF区里出现了LF行。
    async with product(tmp_path) as (root, runtime, provider, *_):
        target = root / "src/service.py"
        target.write_bytes(MIXED)
        read_sha = hashlib.sha256(MIXED).hexdigest()
        multi_line = mixed_edit(
            read_sha,
            "    data = await file.read()\n    if not data:\n",
            "    data = await file.read(limit())\n    if not data:\n",
        )
        provider.steps = (
            tool_step("read_file", "read", path="src/service.py"),
            tool_step("apply_patch_batch", "patch", files=[multi_line]),
            answer("已处理"),
        )
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "限制读取", request_id="first")

        # 模型读到的正文带着原始的\r\n；前置条件按文件的真实字节核对。
        read = next(
            item.content for item in waiting.items if isinstance(item.content, ToolResultContent)
        )
        assert "await file.read()\r\n" in read.output["text"]
        assert read.output["content_sha256"] == read_sha
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        completed = await approve(runtime, thread.thread_id, waiting)
        assert patch_result(completed).outcome == "succeeded"
        edited = MIXED.replace(b"file.read()\r\n", b"file.read(limit())\r\n")
        assert target.read_bytes() == edited
        changes = patch_result(completed).output["changes"]
        assert changes == [
            {"path": "src/service.py", "content_sha256": hashlib.sha256(edited).hexdigest()}
        ]

        # 后续的单行锚点沿用读取时的旧摘要，new_text是多行LF：宿主补当前摘要，写入仍是CRLF。
        single_line = mixed_edit(
            read_sha, "    return data", "    size = len(data)\n    return data"
        )
        provider.steps = (
            tool_step("apply_patch_batch", "second", files=[single_line]),
            answer("已处理"),
        )
        waiting = await runtime.run_turn(thread.thread_id, "记录大小", request_id="second")
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        completed = await approve(runtime, thread.thread_id, waiting)
        assert patch_result(completed).outcome == "succeeded"
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
        provider.steps = (tool_step("apply_patch_batch", "third", files=[copied]), answer("已处理"))
        waiting = await runtime.run_turn(thread.thread_id, "改报错", request_id="third")
        completed = await approve(runtime, thread.thread_id, waiting)
        assert patch_result(completed).outcome == "succeeded"
        final = final.replace(b"ValueError(file)\r\n", b"ValueError(data)\r\n")
        assert target.read_bytes() == final
        # LF区一个字节未动；CRLF区里没有落单的LF。
        assert final.startswith(LF_REGION) and b"\r" not in LF_REGION
        assert final[len(LF_REGION) :].count(b"\n") == final[len(LF_REGION) :].count(b"\r\n")


async def test_a_multi_line_lf_anchor_on_a_crlf_file_gets_the_current_sha(tmp_path):
    # 模型刚改过文件后沿用旧摘要；CRLF文件上的多行锚点只差换行时同样由宿主补当前摘要。
    async with product(tmp_path) as (root, runtime, provider, *_):
        (root / "src/service.py").write_bytes(MIXED)
        stale = mixed_edit(STALE, "    if not data:\n        raise ValueError(file)\n", "")
        _, waiting, call = await run_patch(runtime, provider, root, {"files": [stale]})

        assert waiting.status is TurnStatus.WAITING_APPROVAL
        assert call.arguments["files"][0]["expected_sha256"] == hashlib.sha256(MIXED).hexdigest()
        # 记录与审批的仍是模型发来的文本；换行风格在规划时按文件决定。
        assert call.arguments["files"][0]["edits"] == stale["edits"]
