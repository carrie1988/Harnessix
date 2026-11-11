"""`harnessix --version`直接显示已安装版本，不再报缺少子命令（0.10.0b9）。"""

from __future__ import annotations

from importlib.metadata import version

import pytest

from harnessix.cli import main


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_the_installed_version(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main([flag])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"harnessix {version('harnessix')}"
