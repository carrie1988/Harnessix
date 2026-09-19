from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="当前Evals Schema导入依赖POSIX fcntl；合同生成由Linux和macOS门禁覆盖",
)


def _generator_module() -> ModuleType:
    path = ROOT / "scripts" / "generate_specs.py"
    spec = importlib.util.spec_from_file_location("harnessix_generate_specs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_committed_specs_match_current_contracts() -> None:
    generator = _generator_module()

    assert generator.check_specs(ROOT / "spec") == []


def test_spec_check_detects_missing_and_changed_current_contracts(tmp_path: Path) -> None:
    generator = _generator_module()
    generator.generate_specs(tmp_path)
    target = tmp_path / "agent-event-v20.schema.json"
    target.write_text("{}\n", encoding="utf-8")

    changed = generator.check_specs(tmp_path)
    target.unlink()
    missing = generator.check_specs(tmp_path)

    assert changed == ["已提交合同内容漂移：agent-event-v20.schema.json"]
    assert missing == ["已提交合同缺少生成文件：agent-event-v20.schema.json"]


def test_spec_check_preserves_versioned_historical_contracts(tmp_path: Path) -> None:
    generator = _generator_module()
    generator.generate_specs(tmp_path)
    (tmp_path / "agent-event-v1.schema.json").write_text("{}\n", encoding="utf-8")

    assert generator.check_specs(tmp_path) == []
