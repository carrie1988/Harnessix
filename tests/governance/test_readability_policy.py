from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from harnessix.agent.reducer import apply_event, get_turn, pending_calls, replay, require

ROOT = Path(__file__).parents[2]


def _governance_module() -> ModuleType:
    path = ROOT / "scripts" / "readability_report.py"
    spec = importlib.util.spec_from_file_location("harnessix_readability_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _json(path: str) -> dict[str, object]:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_readability_report_and_policy_match_the_reviewed_tree() -> None:
    governance = _governance_module()
    report = governance.build_report(ROOT / "src" / "harnessix")
    policy = _json("governance/readability-policy-v1.json")

    assert report == _json("docs/baselines/readability-0.9.0-final.json")
    assert governance.check_report(report, policy) == []
    assert report["missing_module_docstrings"] == []
    assert report["undocumented_public_behavior"] == []
    assert report["undocumented_high_risk_callables"] == []

    start = _json("docs/baselines/readability-0.9.0-start.json")
    assert start["source_revision"] == "e15ffaa20142e9f61cf8412b3d4499001a695368"
    assert start["summary"]["source_files"] == 253  # type: ignore[index]
    assert len(start["missing_module_docstrings"]) == 165


def test_readability_policy_reports_independent_regressions_together() -> None:
    governance = _governance_module()
    report = governance.build_report(ROOT / "src" / "harnessix")
    policy = _json("governance/readability-policy-v1.json")
    changed = copy.deepcopy(report)
    changed["missing_module_docstrings"] = ["src/harnessix/new_module.py"]
    changed["undocumented_public_behavior"] = ["harnessix:new_behavior"]
    changed["undocumented_high_risk_callables"] = ["harnessix.agent.runtime:unsafe"]
    changed["oversized_files"].append(  # type: ignore[union-attr]
        {"path": "src/harnessix/new_large.py", "lines": 601}
    )
    changed["package_dependencies"].append("domain->app_server")  # type: ignore[union-attr]
    changed["dependency_cycles"].append(["app_server", "domain"])  # type: ignore[union-attr]
    changed["public_api"]["harnessix"].append("ChangedAPI")  # type: ignore[index,union-attr]

    errors = "\n".join(governance.check_report(changed, policy))
    for expected in (
        "无模块说明",
        "公共行为",
        "高风险入口",
        "新增超大文件",
        "新增一级包依赖边",
        "新增一级包依赖环",
        "公共 __all__ 快照变化",
    ):
        assert expected in errors


def test_reducer_facade_preserves_established_imports_after_split() -> None:
    assert all(
        callable(symbol) for symbol in (apply_event, get_turn, pending_calls, replay, require)
    )
    assert apply_event.__module__ == replay.__module__ == "harnessix.agent.reducer"
    assert len((ROOT / "src/harnessix/agent/reducer.py").read_text().splitlines()) < 300
