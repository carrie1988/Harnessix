"""编码复现草稿：模型可写可运行的复现测试，其内容永不进入交付补丁或完成判断。

两种形态：既有测试目录中的`test_harnessix_scratch*.py`文件（无需新建目录，Django包内亦可作原生标签），
或任意深度的`harnessix_scratch/`目录。
"""

from __future__ import annotations

import re

SCRATCH_DIRECTORY = "harnessix_scratch"
SCRATCH_FILE_PREFIX = "test_harnessix_scratch"
# Git glob 魔术路径：`**/` 匹配零个或多个目录，覆盖仓库根与任意深度。
SCRATCH_EXCLUDE_PATHSPECS = (
    f":(exclude,glob)**/{SCRATCH_DIRECTORY}/**",
    f":(exclude,glob)**/{SCRATCH_FILE_PREFIX}*.py",
)


def is_scratch_path(path: str) -> bool:
    parts = re.split(r"[/\\]", path.rstrip("/\\"))
    return SCRATCH_DIRECTORY in parts or (
        parts[-1].startswith(SCRATCH_FILE_PREFIX) and parts[-1].endswith(".py")
    )
