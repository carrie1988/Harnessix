"""评测前工作区权限规整：只改普通文件的权限位，Git看不到任何变化。"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from integrations.harbor.workspace_modes import normalize_modes_command


@pytest.mark.skipif(os.name != "posix", reason="POSIX文件权限")
def test_normalized_modes_are_invisible_to_git(tmp_path: Path) -> None:
    """E0c的0777镜像：规整为0644/0755后Git无任何变化，.git与符号链接不受影响。"""
    repo = tmp_path / "testbed"
    (repo / "pkg").mkdir(parents=True)
    script, module = repo / "run.sh", repo / "pkg" / "module.py"
    script.write_text("#!/bin/sh\n")
    module.write_text("value = 1\n")
    (repo / "link.py").symlink_to("pkg/module.py")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    script.chmod(0o777)
    module.chmod(0o666)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    git_config = (repo / ".git" / "config").stat().st_mode

    subprocess.run(["/bin/sh", "-c", normalize_modes_command(str(repo))], check=True)

    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert stat.S_IMODE(module.stat().st_mode) == 0o644
    assert (repo / "link.py").is_symlink()
    assert (repo / ".git" / "config").stat().st_mode == git_config
    status = subprocess.run(
        [*git, "status", "--porcelain"], check=True, capture_output=True, text=True
    )
    assert status.stdout == ""
