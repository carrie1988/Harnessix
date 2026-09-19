"""阻止独立Action服务重新进入Harnessix Code公共产品面。"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import harnessix
import harnessix.sdk as sdk

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src" / "harnessix"

_LEGACY_MODULES = {
    "harnessix.bootstrap",
    "harnessix.runtime",
    "harnessix.worker",
}
_RETIRED_PATHS = (
    "adapters",
    "api",
    "bootstrap.py",
    "domain/ports.py",
    "domain/registry.py",
    "executors",
    "policy",
    "runtime.py",
    "sdk/client.py",
    "settings.py",
    "storage",
    "worker.py",
    "artifacts/process_output.py",
    "processes/action_executor.py",
    "processes/agent_bridge.py",
    "processes/agent_runtime.py",
    "processes/session_projection.py",
    "processes/test_profiles.py",
)


def _legacy_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _LEGACY_MODULES:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(item.name for item in node.names if item.name in _LEGACY_MODULES)
    return imports


def test_public_python_surface_only_exports_agent_sdk_clients() -> None:
    retired = {"HarnessixAPIError", "HarnessixAsyncClient", "HarnessixClient"}

    assert retired.isdisjoint(harnessix.__all__)
    assert harnessix.__all__ == []
    assert retired.isdisjoint(sdk.__all__)
    assert {
        "AgentClient",
        "AgentSDKError",
        "AgentTransport",
        "InProcessAgentTransport",
        "SubprocessAgentTransport",
    }.issubset(sdk.__all__)


def test_legacy_action_runtime_has_no_production_callers_or_source_tree() -> None:
    observed = {
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*.py")
        if _legacy_imports(path)
    }

    assert observed == set()
    assert all(not (SOURCE / path).exists() for path in _RETIRED_PATHS)


def test_default_product_composition_does_not_import_legacy_action_runtime() -> None:
    product_files = tuple((SOURCE / "product_config").glob("*.py")) + tuple(
        (SOURCE / "product_ui").glob("*.py")
    )

    assert all(not _legacy_imports(path) for path in product_files)


def test_legacy_service_dependencies_are_not_base_runtime_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base_names = {
        requirement.split(">", 1)[0].split("=", 1)[0] for requirement in project["dependencies"]
    }
    retired_service_names = {"asyncpg", "fastapi", "langchain-core", "uvicorn"}

    assert retired_service_names.isdisjoint(base_names)
    assert "legacy-action" not in project["optional-dependencies"]


def test_retired_action_examples_and_make_target_are_absent() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert not (ROOT / "examples" / "mvp.py").exists()
    assert not (ROOT / "examples" / "langgraph_tool.py").exists()
    assert not (ROOT / "examples" / "process_action.py").exists()
    assert not (ROOT / "examples" / "kernel_process.py").exists()
    assert not (ROOT / "examples" / "coding_feedback.py").exists()
    assert not (ROOT / "spec" / "action-contract-v1.schema.json").exists()
    assert not (ROOT / "spec" / "openapi.json").exists()
    assert "examples/mvp.py" not in makefile
    assert "demo:" not in makefile
