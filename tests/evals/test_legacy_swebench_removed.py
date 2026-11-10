"""旧本地SWE-bench路径已删除：顶层CLI不再提供其子命令，产品包不含基准专用模块和复现门禁。"""

from __future__ import annotations

from pathlib import Path

import pytest

import harnessix
from harnessix.agent.ports import ModelToolCallAdmission
from harnessix.cli import main
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView

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


def test_dormant_reproduction_gate_and_prompt_are_gone(tmp_path: Path) -> None:
    assert not (PACKAGE_ROOT / "product_config" / "coding_reproduction_gate.py").exists()
    # 工作流视图不再实现调用准入，Runtime按普通视图处理。
    assert not isinstance(CodingWorkflowToolView(), ModelToolCallAdmission)
    with pytest.raises(TypeError):
        CodingWorkflowToolView(require_failing_reproduction=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        CodingWorkflowToolView(scratch_reproduction=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_product_agent_context(tmp_path, reproduction_guidance=True)  # type: ignore[call-arg]


@pytest.mark.parametrize("marker", ["runtests", "bin/test"])
def test_product_package_has_no_repository_specific_test_runner_rules(marker: str) -> None:
    # Django的tests/runtests.py与SymPy的bin/test只服务旧基准适配，产品源码不再识别它们。
    offending = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in _product_sources()
        if marker in path.read_text(encoding="utf-8")
    ]
    assert offending == []
