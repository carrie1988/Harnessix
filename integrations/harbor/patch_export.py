"""智能体阶段结束时导出它对工作区的全部改动，供官方SWE-bench判分使用。

Harbor的验收直接检查容器内工作区，官方判分则需要补丁；两者必须基于同一时刻的工作区，
因此补丁在智能体运行结束、验收开始之前导出。

基线是智能体开始前的工作区快照，不是HEAD：部分官方镜像的/testbed在开始前就有未跟踪的构建产物
（如Go的*.test）或已修改的依赖清单（如package-lock.json），它们不是智能体的改动，混进补丁会让
官方判分在同一镜像上应用补丁失败。快照用以HEAD为底的临时索引执行`git add -A`再写树：
已跟踪但被忽略的文件留在索引中（空白临时索引会把它们算成删除），忽略的未跟踪文件不进入快照。
导出是两次快照之间的树差异；不改动工作区和它的Git索引，只向对象库写入快照用到的对象。
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

PATCH_FILE = "patch.diff"
_HEX = frozenset("0123456789abcdef")


def _index_from_workspace(quoted: str) -> str:
    """在新建的临时目录里以HEAD为底建临时索引并加入工作区全部改动；临时索引路径必须尚不存在。"""
    return (
        'scratch=$(mktemp -d) && export GIT_INDEX_FILE="$scratch/index" && '
        f"git -C {quoted} read-tree HEAD && git -C {quoted} add -A"
    )


def workspace_tree_command(workspace: str) -> str:
    """输出当前工作区快照的树对象ID。"""
    quoted = shlex.quote(workspace)
    return (
        f"{_index_from_workspace(quoted)} && git -C {quoted} write-tree; "
        'status=$?; rm -rf "$scratch"; exit $status'
    )


def parse_object_id(stdout: object) -> str | None:
    value = stdout.strip() if isinstance(stdout, str) else ""
    return value if len(value) in (40, 64) and set(value) <= _HEX else None


def patch_command(workspace: str, base_tree: str, logs_dir: PurePosixPath) -> str:
    """把结束时的快照与开始时的快照（base_tree）比较，写出二进制安全的补丁。"""
    if parse_object_id(base_tree) is None:
        raise ValueError("基线必须是树对象ID")
    quoted, target = shlex.quote(workspace), shlex.quote(str(logs_dir / PATCH_FILE))
    return (
        f"{_index_from_workspace(quoted)} && end=$(git -C {quoted} write-tree) && "
        f'unset GIT_INDEX_FILE && git -C {quoted} diff --binary {base_tree} "$end" > {target}; '
        'status=$?; rm -rf "$scratch"; exit $status'
    )
