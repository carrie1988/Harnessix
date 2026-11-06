"""评测平台核心保持系统无关：源码不得导入harnessix，运行时也不加载它。"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVALKIT = ROOT / "packages" / "evalkit" / "src" / "evalkit"


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_evalkit_sources_never_import_harnessix() -> None:
    sources = sorted(EVALKIT.rglob("*.py"))
    assert sources
    offenders = [str(p.relative_to(ROOT)) for p in sources if "harnessix" in _imported_roots(p)]
    assert offenders == []


def test_importing_evalkit_does_not_load_harnessix() -> None:
    modules = sorted(
        ".".join(("evalkit", *p.relative_to(EVALKIT).with_suffix("").parts))
        for p in EVALKIT.rglob("*.py")
        if p.stem not in {"__init__", "__main__"}
    )
    code = f"import sys\nsys.path.insert(0, {str(EVALKIT.parent)!r})\n"
    code += "".join(f"import {m}\n" for m in modules)
    code += "assert not [m for m in sys.modules if m.split('.')[0] == 'harnessix'], 'loaded'\n"
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
