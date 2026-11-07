"""评测前把任务工作区的文件权限规整为Git风格的0644/0755（ADR 0120 §7.1）。

部分SWE-bench官方镜像把全部文件设为0777，而Harnessix补丁事务目前只修改0644/0755的文件
（产品层面放开该限制另行实施）。Git只按属主可执行位区分100644与100755，按同一规则规整后
Git看不到任何变化，补丁与判分不受影响；一期旧评测运行器准备工作区时做同样处理。
只处理普通文件：不进入.git，不跟随符号链接，不改目录，不改文件内容。
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath


def normalize_modes_command(workspace: str) -> str:
    root = shlex.quote(workspace)
    git_dir = shlex.quote(str(PurePosixPath(workspace) / ".git"))
    return (
        f"find {root} -path {git_dir} -prune -o "
        "-type f -perm -0100 ! -perm 0755 -exec chmod 0755 {} + -o "
        "-type f ! -perm -0100 ! -perm 0644 -exec chmod 0644 {} +"
    )
