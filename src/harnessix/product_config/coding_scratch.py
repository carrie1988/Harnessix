"""复现草稿命名约定：`test_harnessix_scratch*.py`文件或任意深度的`harnessix_scratch/`目录。

目前只有候选交叉检查用它识别并叠加另一候选的草稿文件；产品工作流视图与补丁导出不对草稿做特殊处理。
"""

from __future__ import annotations

import re

SCRATCH_DIRECTORY = "harnessix_scratch"
SCRATCH_FILE_PREFIX = "test_harnessix_scratch"


def is_scratch_path(path: str) -> bool:
    parts = re.split(r"[/\\]", path.rstrip("/\\"))
    return SCRATCH_DIRECTORY in parts or (
        parts[-1].startswith(SCRATCH_FILE_PREFIX) and parts[-1].endswith(".py")
    )
