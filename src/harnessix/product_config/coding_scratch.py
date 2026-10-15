"""编码复现草稿目录：模型可在其中写并运行复现测试，但其内容永不进入交付补丁或完成判断。"""

from __future__ import annotations

import re

SCRATCH_DIRECTORY = "harnessix_scratch"
# Git glob 魔术路径：`**/` 匹配零个或多个目录，因此同时覆盖仓库根与任意深度的草稿目录。
SCRATCH_EXCLUDE_PATHSPEC = f":(exclude,glob)**/{SCRATCH_DIRECTORY}/**"


def is_scratch_path(path: str) -> bool:
    return SCRATCH_DIRECTORY in re.split(r"[/\\]", path.rstrip("/\\"))
