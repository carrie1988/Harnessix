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

_CURRENT_DOCUMENTATION_EXPECTATIONS = {
    "docs/build-vs-buy.md": (
        ("Adapter 使用 Harnessix Action Plane", "| Action Plane | 自研 |"),
        ("Trusted Action Runtime", "Agent Protocol驱动Harnessix Code"),
    ),
    "docs/modules/session.md": (
        ("Session与Action Plane Effect Journal是两套独立存储",),
        ("Session与Trusted Action", "已删除Action Plane"),
    ),
    "docs/modules/protocol.md": (
        ("Trusted Actions和Action Plane负责",),
        ("Trusted Action Runtime及具体能力的效果Owner",),
    ),
    "docs/modules/skills.md": (
        ("为什么仍经过Action Plane", "统一Action路径提供"),
        ("为什么仍经过Trusted Action Runtime", "统一Trusted Action路径提供"),
    ),
    "docs/modules/smoke.md": (
        ("读取Action Plane Settings", "不读取Action Plane环境变量"),
        ("解析其余产品命令", "不读取场景文件、Provider凭据或遗留服务环境变量"),
    ),
    "docs/modules/delivery.md": (
        ("只描述待删除的迁移兼容内核",),
        ("只保存已删除体系的冻结历史",),
    ),
    "docs/modules/execution.md": (
        ("相邻当前事实：[Action Plane子系统设计]",),
        ("相邻当前事实：[Trusted Actions模块设计]",),
    ),
    "docs/modules/trusted-actions.md": (
        ("兼容Action Plane继续承诺", "与Execution及兼容Action Plane的关系"),
        ("与Execution及已删除Action Plane的历史边界", "仅离线归档和历史文档"),
    ),
    "docs/modules/hooks.md": (
        ("在统一Action Plane之外形成第二条执行通道",),
        ("在Trusted Action Runtime之外形成第二条执行通道",),
    ),
    "docs/modules/secrets.md": (
        ("Action Plane的Secret合同不可直接用于Coding Tool",),
        ("遗留Action SecretRef不是当前执行合同",),
    ),
    "docs/roadmap.md": (
        ("## 2. 当前基线：0.1 Action Plane", "为Action Plane补齐Policy/Executor/Reconcile"),
        (
            "## 2. 历史基线：0.1 Action Plane",
            "为Trusted Action Runtime补齐Policy/Executor/Reconcile",
        ),
    ),
}


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


def test_current_documentation_does_not_revive_retired_action_plane() -> None:
    """现行事实源不得把已删除服务写成仍可部署或仍参与运行。"""

    for relative_path, (
        retired_fragments,
        required_fragments,
    ) in _CURRENT_DOCUMENTATION_EXPECTATIONS.items():
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert all(fragment not in content for fragment in retired_fragments), relative_path
        assert all(fragment in content for fragment in required_fragments), relative_path


def test_historical_action_docs_do_not_advertise_retired_runtime() -> None:
    """历史合同和README迁移叙述不得重新宣传已删除的执行入口。"""

    contract = (ROOT / "docs" / "action-contract.md").read_text(encoding="utf-8")
    subsystem = (ROOT / "docs" / "subsystems" / "action-plane.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    historical_process = readme.split("### 持久命令准入（0.5.4b1历史实现，已于0.9.1f3删除）", 1)[
        1
    ].split("## 当前已实现：Git与受控测试反馈", 1)[0]

    assert "status: historical" in contract
    assert "当前版本不接受该合同" in contract
    assert "status: deprecated" in subsystem
    assert "当前源码没有可调用的旧运行时" in subsystem
    assert "宿主现在可以用`process_action_tool(factory)`" not in historical_process
    assert "该入口在当前版本中不可调用" in historical_process


def test_pytest_subsets_can_import_repository_test_support() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "." in configuration["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert (ROOT / "tests" / "__init__.py").is_file()
