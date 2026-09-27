"""合同导入不依赖POSIX执行模块；公共Eval导出保持原对象身份。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BLOCKER = """
import importlib.abc
import sys
class RejectExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            'harnessix.evals.campaign_execution',
            'harnessix.evals.delivery',
            'harnessix.evals.runner',
            'harnessix.evals.suite_execution',
            'harnessix.evals.provider_suite_execution',
            'harnessix.patches.managed',
        }:
            raise AssertionError('contract_import_loaded_posix_execution: ' + fullname)
        return None
sys.meta_path.insert(0, RejectExecutionImports())
"""


@pytest.mark.parametrize("mode", ["contracts", "help", "check"])
def test_contracts_and_generator_never_load_posix_execution(mode: str) -> None:
    body = (
        "import harnessix.evals.campaign_contracts; import harnessix.evals.delivery_contracts; "
        "import harnessix.evals as evals; assert set(evals.__all__) <= set(dir(evals))"
        if mode == "contracts"
        else (
            "import runpy; sys.argv=['generate_specs', '--"
            + mode
            + "']; runpy.run_module('scripts.generate_specs', run_name='__main__')"
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", BLOCKER + body],
        cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"},
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    if mode == "check":
        assert "合同一致性检查通过" in completed.stdout


@pytest.mark.skipif(
    sys.platform == "win32", reason="原POSIX执行入口不提供Windows实现；合同导入独立验收"
)
def test_lazy_exports_keep_original_objects_and_unknown_names_fail() -> None:
    from importlib import import_module

    import harnessix.evals as evals

    for name, module_name in evals._LAZY_EXECUTION_EXPORTS.items():
        original = getattr(import_module(module_name), name)
        assert name in evals.__all__
        assert getattr(evals, name) is original
        assert getattr(evals, name) is original
    with pytest.raises(AttributeError):
        _ = evals.not_a_public_eval_export
