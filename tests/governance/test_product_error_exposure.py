"""产品代码不得把第三方异常原文写入日志或公开输出。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "harnessix"
# 评测是开发者工具，不进入用户的产品路径。
EXCLUDED = {"evals"}
LOG_METHODS = {"exception", "error", "warning", "critical"}


def _product_modules() -> list[Path]:
    return [path for path in ROOT.rglob("*.py") if path.relative_to(ROOT).parts[0] not in EXCLUDED]


def test_product_code_does_not_log_or_print_tracebacks() -> None:
    offenders: list[str] = []
    for path in _product_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            name = owner.id if isinstance(owner, ast.Name) else ""
            if name == "traceback" or (
                name in {"logger", "logging"} and node.func.attr in LOG_METHODS
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_generic_handler_does_not_expose_the_exception_text() -> None:
    source = (ROOT / "app_server" / "server.py").read_text(encoding="utf-8")
    assert "服务端内部错误；原始异常未公开" in source
