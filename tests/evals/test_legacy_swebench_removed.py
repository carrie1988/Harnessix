"""旧本地SWE-bench路径已删除：顶层CLI不再提供其子命令，产品包不含基准专用模块。"""

from __future__ import annotations

from pathlib import Path

import pytest

import harnessix
from harnessix.cli import main

PACKAGE_ROOT = Path(harnessix.__file__).parent
RETIRED_COMMAND = "coding-eval-swebench"


def _product_sources() -> list[Path]:
    # 只看已跟踪形态的源码文件；残留的__pycache__目录不代表模块仍随包发布。
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_cli_help_no_longer_lists_the_retired_command(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert RETIRED_COMMAND not in capsys.readouterr().out


def test_cli_rejects_the_retired_command_as_unknown(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main([RETIRED_COMMAND, "--help"])
    assert raised.value.code == 2
    assert f"invalid choice: '{RETIRED_COMMAND}'" in capsys.readouterr().err


def test_product_package_ships_no_swebench_module() -> None:
    sources = _product_sources()
    assert sources, "未找到产品包源码"
    offending = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in sources
        if any("swebench" in part for part in path.relative_to(PACKAGE_ROOT).parts)
    ]
    assert offending == []
