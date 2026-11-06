"""智能体阶段结束时导出相对运行前HEAD的完整补丁，供官方SWE-bench判分使用。

Harbor的验收直接检查容器内工作区，官方判分则需要补丁；两者必须基于同一时刻的工作区，
因此补丁在智能体运行结束、验收开始之前导出。使用临时索引，包含新文件，不改动工作区的Git索引。
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

PATCH_FILE = "patch.diff"
_HEX = frozenset("0123456789abcdef")


def head_command(workspace: str) -> str:
    return shlex.join(["git", "-C", workspace, "rev-parse", "HEAD"])


def parse_head(stdout: object) -> str | None:
    head = stdout.strip() if isinstance(stdout, str) else ""
    return head if len(head) in (40, 64) and set(head) <= _HEX else None


def patch_command(workspace: str, base: str, logs_dir: PurePosixPath) -> str:
    quoted, target = shlex.quote(workspace), shlex.quote(str(logs_dir / PATCH_FILE))
    # 临时索引必须是尚不存在的路径：git把已存在的空文件视为损坏的索引。
    return (
        f'scratch=$(mktemp -d) && GIT_INDEX_FILE="$scratch/index" git -C {quoted} add -A && '
        f'GIT_INDEX_FILE="$scratch/index" git -C {quoted} diff --cached --binary {base} '
        f'> {target}; status=$?; rm -rf "$scratch"; exit $status'
    )
