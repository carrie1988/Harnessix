"""从模型历史提取文件摘要：只认宿主写入的成功结果，同一路径以最后一次观察为准。"""

from __future__ import annotations

from uuid import uuid4

from harnessix.agent.file_observations import observed_file_digests
from harnessix.agent.models import (
    Item,
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.domain.models import EffectClass

A, B, C = "a" * 64, "b" * 64, "c" * 64


def observation(tool, output, outcome="succeeded"):
    """一次工具调用及其结果，形状与会话历史中的Item相同。"""
    call_id = uuid4()
    call = ToolCallContent(
        call_id=call_id,
        provider_call_id=f"provider-{call_id}",
        tool=tool,
        tool_version="1.test",
        effect_class=EffectClass.READ_ONLY,
    )
    result = ToolResultContent(call_id=call_id, outcome=outcome, output=output)
    return [
        Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)
        for content in (call, result)
    ]


def read(path, sha256):
    return observation("read_file", {"path": path, "text": "x\n", "content_sha256": sha256})


def patched(*changes):
    files = [{"path": path, "content_sha256": sha256} for path, sha256 in changes]
    return observation("apply_patch_batch", {"state": "published", "changes": files})


def test_complete_reads_grep_snapshots_and_patch_results_are_observations():
    grep_output = {
        "preview": {"matches": [{"path": "src/b.py", "line": 3, "content_sha256": B}]},
        "artifact": {"artifact_id": str(uuid4())},
    }
    history = [
        *read("src/a.py", A),
        *observation("grep", grep_output),
        *patched(("src/c.py", C)),
    ]

    assert observed_file_digests(history) == {"src/a.py": A, "src/b.py": B, "src/c.py": C}


def test_the_latest_observation_of_a_path_wins():
    history = [*read("src/a.py", A), *patched(("src/a.py", B)), *read("src/a.py", C)]

    assert observed_file_digests(history) == {"src/a.py": C}


def test_deleted_files_and_omitted_digests_are_no_longer_observed():
    # 删除后的content_sha256为null；超过摘要上限的读取同样是null：都不能再当作前置条件。
    history = [
        *read("src/a.py", A),
        *read("big.bin", B),
        *patched(("src/a.py", None)),
        *read("big.bin", None),
    ]

    assert observed_file_digests(history) == {}


def test_only_successful_host_results_with_a_full_digest_count():
    user_text = Item(
        item_id=uuid4(),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="user_message", text=f"src/a.py content_sha256 {A}"),
    )
    history = [
        user_text,
        *observation("read_file", {"path": "src/a.py", "content_sha256": A}, outcome="failed"),
        # 原字面量grep的命中只有revision，没有完整内容摘要。
        *observation("grep", {"matches": [{"path": "src/b.py", "line": 1, "revision": B}]}),
        # 其他工具的输出即使字段同名也不是文件观察。
        *observation("exec_command.shell", {"path": "src/c.py", "content_sha256": C}),
        *observation("read_file", "not an object"),
    ]

    assert observed_file_digests(history) == {}
