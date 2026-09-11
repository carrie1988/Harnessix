"""生成并校验 Harnessix 源码可读性与结构治理基线。"""

from __future__ import annotations

import argparse
import ast
import io
import json
import tokenize
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORT_VERSION = "harnessix.readability-report/v1"
POLICY_VERSION = "harnessix.readability-policy/v1"
DEFAULT_SOURCE_ROOT = Path("src/harnessix")
DEFAULT_POLICY = Path("governance/readability-policy-v1.json")
DEFAULT_FINAL_REPORT = Path("docs/baselines/readability-0.9.0-final.json")
FILE_LINE_THRESHOLD = 600
SYMBOL_LINE_THRESHOLD = 100
DECISION_COMPLEXITY_THRESHOLD = 20
HIGH_RISK_SYMBOL_GROUPS = (
    ("harnessix.agent.reducer:apply_event",),
    ("harnessix.agent.reducer:replay",),
    (
        "harnessix.agent.item_reducer:_start_item",
        "harnessix.agent.reducer:_start_item",
    ),
    (
        "harnessix.agent.turn_reducer:_change_state",
        "harnessix.agent.reducer:_change_state",
    ),
    ("harnessix.agent.runtime:AgentRuntime",),
    ("harnessix.agent.runtime:AgentRuntime._accept",),
    ("harnessix.agent.runtime:AgentRuntime._commit",),
    ("harnessix.agent.runtime:AgentRuntime._drive",),
    ("harnessix.agent.runtime:AgentRuntime._execute_calls",),
    ("harnessix.agent.runtime:AgentRuntime._finish",),
    ("harnessix.agent.runtime:AgentRuntime._recover",),
    ("harnessix.agent.runtime:AgentRuntime._reply_approval",),
    ("harnessix.agent.runtime:AgentRuntime._run_compaction",),
    ("harnessix.agent.runtime:AgentRuntime._sample_events",),
    ("harnessix.delivery.filesystem:WorkspaceTransactionRuntime.publish",),
    ("harnessix.delivery.filesystem:WorkspaceTransactionRuntime.reconcile",),
    ("harnessix.delivery.git:GitDeliveryRuntime",),
    ("harnessix.delivery.git:GitDeliveryRuntime.commit",),
    ("harnessix.delivery.git:GitDeliveryRuntime.create_checkpoint",),
    ("harnessix.delivery.git:GitDeliveryRuntime.create_worktree",),
    ("harnessix.patches.managed:ManagedPatchWorkspace.execute",),
    ("harnessix.processes.supervisor:SupervisedProcess.refresh",),
    ("harnessix.processes.supervisor:PosixProcessSupervisor.start",),
    ("harnessix.trusted_actions.router:TrustedActionRouter.execute",),
    ("harnessix.trusted_actions.router:TrustedActionRouter.plan",),
)

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_DOCUMENTABLE_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_IGNORED_TOKENS = {
    tokenize.ENCODING,
    tokenize.ENDMARKER,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.NEWLINE,
    tokenize.NL,
    tokenize.COMMENT,
}


@dataclass(frozen=True)
class ModuleSource:
    """保存一次静态分析所需的模块路径、源码和语法树。"""

    name: str
    path: str
    text: str
    tree: ast.Module


class _DecisionComplexity(ast.NodeVisitor):
    """计算不穿透嵌套定义的确定性分支复杂度。"""

    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.score = 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self.score += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.score += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.score += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.score += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.score += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.score += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.score += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.score += len(node.handlers) + bool(node.orelse) + bool(node.finalbody)
        self.generic_visit(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self.visit_Try(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.score += sum(
            not (isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None)
            for case in node.cases
        )
        self.generic_visit(node)


def _module_name(source_root: Path, path: Path) -> str:
    parts = [source_root.name, *path.relative_to(source_root).with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _load_modules(source_root: Path) -> dict[str, ModuleSource]:
    modules: dict[str, ModuleSource] = {}
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        name = _module_name(source_root, path)
        stable_path = Path("src") / source_root.name / path.relative_to(source_root)
        modules[name] = ModuleSource(
            name=name,
            path=stable_path.as_posix(),
            text=text,
            tree=ast.parse(text, filename=str(path)),
        )
    return modules


def _logical_lines(text: str) -> int:
    lines = {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type not in _IGNORED_TOKENS
    }
    return len(lines)


def _decision_complexity(node: ast.AST) -> int:
    visitor = _DecisionComplexity(node)
    visitor.visit(node)
    return visitor.score


def _symbol_records(module: ModuleSource) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def visit(nodes: Iterable[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if not isinstance(node, _DOCUMENTABLE_NODES):
                continue
            qualname = ".".join((*parents, node.name))
            end_line = node.end_lineno or node.lineno
            record: dict[str, Any] = {
                "id": f"{module.name}:{qualname}",
                "module": module.name,
                "path": module.path,
                "qualname": qualname,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "line": node.lineno,
                "lines": end_line - node.lineno + 1,
                "documented": ast.get_docstring(node, clean=False) is not None,
            }
            if isinstance(node, _FUNCTION_NODES):
                record["decision_complexity"] = _decision_complexity(node)
            records.append(record)
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*parents, node.name))

    visit(module.tree.body)
    return records


def _literal_all(module: ModuleSource) -> tuple[str, ...] | None:
    for node in module.tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            value = node.value
        if value is None:
            continue
        try:
            names = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            raise ValueError(f"{module.path} 的 __all__ 必须是静态字符串序列") from None
        if not isinstance(names, (list, tuple)) or not all(isinstance(name, str) for name in names):
            raise ValueError(f"{module.path} 的 __all__ 必须是静态字符串序列")
        return tuple(names)
    return None


def _public_api(
    modules: dict[str, ModuleSource], symbol_records: list[dict[str, Any]]
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    definitions: dict[tuple[str, str], dict[str, Any]] = {
        (record["module"], record["qualname"]): record
        for record in symbol_records
        if "." not in record["qualname"]
    }
    definition_nodes: dict[tuple[str, str], ast.AST] = {
        (module.name, node.name): node
        for module in modules.values()
        for node in module.tree.body
        if isinstance(node, _DOCUMENTABLE_NODES)
    }
    imports: dict[tuple[str, str], tuple[str, str]] = {}
    for module in modules.values():
        for node in module.tree.body:
            if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
                continue
            for alias in node.names:
                imports[(module.name, alias.asname or alias.name)] = (node.module, alias.name)

    def resolve(module_name: str, name: str) -> tuple[str, str] | None:
        seen: set[tuple[str, str]] = set()
        current = (module_name, name)
        while current not in seen:
            seen.add(current)
            if current in definitions:
                return current
            target = imports.get(current)
            if target is None:
                return None
            current = target
        return None

    def is_data_contract(key: tuple[str, str], seen: frozenset[tuple[str, str]]) -> bool:
        if key in seen:
            return False
        node = definition_nodes[key]
        if not isinstance(node, ast.ClassDef):
            return False
        for base in node.bases:
            if isinstance(base, ast.Subscript):
                base = base.value
            leaf: str | None = None
            if isinstance(base, ast.Name):
                leaf = base.id
            elif isinstance(base, ast.Attribute):
                leaf = base.attr
            if leaf in {"BaseModel", "RootModel", "Enum", "IntEnum", "StrEnum"}:
                return True
            if leaf is None:
                continue
            target = (
                (key[0], leaf)
                if (key[0], leaf) in definition_nodes
                else imports.get((key[0], leaf))
            )
            if target in definition_nodes and is_data_contract(target, seen | {key}):
                return True
        return False

    exports: dict[str, list[str]] = {}
    undocumented: list[str] = []
    contract_types: list[str] = []
    for module_name, module in sorted(modules.items()):
        names = _literal_all(module)
        if names is None:
            continue
        exports[module_name] = sorted(names)
        for name in names:
            key = resolve(module_name, name)
            if key is None:
                continue
            record = definitions[key]
            if is_data_contract(key, frozenset()):
                contract_types.append(f"{module_name}:{name}->{record['id']}")
            elif not record["documented"]:
                undocumented.append(f"{module_name}:{name}->{record['id']}")
    return exports, sorted(undocumented), sorted(contract_types)


def _package_name(module_name: str) -> str:
    parts = module_name.split(".")
    return parts[1] if len(parts) > 1 else "__root__"


def _package_dependencies(modules: dict[str, ModuleSource]) -> list[str]:
    edges: set[tuple[str, str]] = set()
    for module in modules.values():
        source_package = _package_name(module.name)
        for node in ast.walk(module.tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported = [node.module]
            for target in imported:
                if target != "harnessix" and not target.startswith("harnessix."):
                    continue
                target_package = _package_name(target)
                if source_package != target_package:
                    edges.add((source_package, target_package))
    return [f"{source}->{target}" for source, target in sorted(edges)]


def _dependency_cycles(edges: list[str]) -> list[list[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source, target = edge.split("->", 1)
        graph[source].add(target)
        graph.setdefault(target, set())

    index = 0
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    stacked: set[str] = set()
    components: list[list[str]] = []

    def connect(node: str) -> None:
        nonlocal index
        indices[node] = low_links[node] = index
        index += 1
        stack.append(node)
        stacked.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                connect(target)
                low_links[node] = min(low_links[node], low_links[target])
            elif target in stacked:
                low_links[node] = min(low_links[node], indices[target])
        if low_links[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            stacked.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1:
            components.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            connect(node)
    return sorted(components)


def build_report(source_root: Path, *, revision: str | None = None) -> dict[str, Any]:
    """只读取源码并返回排序稳定、与平台无关的治理报告。"""
    modules = _load_modules(source_root)
    symbols = [record for module in modules.values() for record in _symbol_records(module)]
    exports, undocumented_exports, public_contract_types = _public_api(modules, symbols)
    symbols_by_id = {record["id"]: record for record in symbols}
    high_risk_symbols = tuple(
        next((symbol_id for symbol_id in group if symbol_id in symbols_by_id), "")
        for group in HIGH_RISK_SYMBOL_GROUPS
    )
    absent_high_risk = [
        " | ".join(group)
        for group, found in zip(HIGH_RISK_SYMBOL_GROUPS, high_risk_symbols, strict=True)
        if not found
    ]
    if absent_high_risk:
        raise ValueError("高风险入口不存在或已改名: " + ", ".join(absent_high_risk))
    undocumented_high_risk = sorted(
        symbol_id for symbol_id in high_risk_symbols if not symbols_by_id[symbol_id]["documented"]
    )
    module_rows = [
        {
            "module": module.name,
            "path": module.path,
            "physical_lines": len(module.text.splitlines()),
            "logical_lines": _logical_lines(module.text),
            "documented": ast.get_docstring(module.tree, clean=False) is not None,
        }
        for module in modules.values()
    ]
    package_edges = _package_dependencies(modules)
    report: dict[str, Any] = {
        "spec_version": REPORT_VERSION,
        "metric_definition": {
            "physical_lines": "splitlines 后的源码行数",
            "logical_lines": "至少包含一个非注释、非布局 Token 的源码行数",
            "decision_complexity": (
                "函数基础值1，加 if/for/while/条件表达式/推导式、布尔分支、"
                "try分支和非默认match分支；不穿透嵌套定义"
            ),
            "symbols": "模块顶层类、函数及类方法；不重复统计函数内的嵌套定义",
            "public_api": "源码中静态 __all__ 导出的名称快照",
            "public_behavior": "__all__ 导出且不是Pydantic合同或Enum的可解析类与函数",
            "public_contract_type": (
                "__all__ 导出的Pydantic合同或Enum；语义由字段、校验器及冻结Schema记录"
            ),
            "dependency_cycle": "harnessix 一级包静态导入图的强连通分量",
        },
        "thresholds": {
            "file_lines": FILE_LINE_THRESHOLD,
            "symbol_lines": SYMBOL_LINE_THRESHOLD,
            "decision_complexity": DECISION_COMPLEXITY_THRESHOLD,
        },
        "summary": {
            "source_files": len(module_rows),
            "physical_lines": sum(row["physical_lines"] for row in module_rows),
            "logical_lines": sum(row["logical_lines"] for row in module_rows),
            "documented_modules": sum(row["documented"] for row in module_rows),
            "classes": sum(record["kind"] == "class" for record in symbols),
            "documented_classes": sum(
                record["kind"] == "class" and record["documented"] for record in symbols
            ),
            "functions": sum(record["kind"] == "function" for record in symbols),
            "documented_functions": sum(
                record["kind"] == "function" and record["documented"] for record in symbols
            ),
            "public_exports": sum(len(names) for names in exports.values()),
            "public_contract_types": len(public_contract_types),
            "undocumented_public_behavior": len(undocumented_exports),
            "high_risk_callables": len(high_risk_symbols),
            "undocumented_high_risk_callables": len(undocumented_high_risk),
            "package_dependency_edges": len(package_edges),
        },
        "missing_module_docstrings": sorted(
            row["path"] for row in module_rows if not row["documented"]
        ),
        "public_contract_types": public_contract_types,
        "undocumented_public_behavior": undocumented_exports,
        "undocumented_high_risk_callables": undocumented_high_risk,
        "oversized_files": sorted(
            (
                {"path": row["path"], "lines": row["physical_lines"]}
                for row in module_rows
                if row["physical_lines"] > FILE_LINE_THRESHOLD
            ),
            key=lambda row: row["path"],
        ),
        "oversized_symbols": sorted(
            (
                record
                for record in symbols
                if record["lines"] > SYMBOL_LINE_THRESHOLD
                or record.get("decision_complexity", 0) > DECISION_COMPLEXITY_THRESHOLD
            ),
            key=lambda record: record["id"],
        ),
        "package_dependencies": package_edges,
        "dependency_cycles": _dependency_cycles(package_edges),
        "public_api": exports,
    }
    if revision is not None:
        report["source_revision"] = revision
    return report


def build_policy(report: dict[str, Any]) -> dict[str, Any]:
    """把已评审报告转换为仅允许债务收敛的精确门禁。"""
    return {
        "spec_version": POLICY_VERSION,
        "report_spec_version": report["spec_version"],
        "thresholds": report["thresholds"],
        "required_module_docstrings": True,
        "required_public_behavior_docstrings": True,
        "required_high_risk_callable_docstrings": True,
        "approved_oversized_files": {
            item["path"]: item["lines"] for item in report["oversized_files"]
        },
        "approved_oversized_symbols": {
            item["id"]: {
                "max_lines": item["lines"],
                "max_decision_complexity": item.get("decision_complexity", 0),
            }
            for item in report["oversized_symbols"]
        },
        "approved_package_dependencies": report["package_dependencies"],
        "approved_dependency_cycles": report["dependency_cycles"],
        "public_api": report["public_api"],
    }


def check_report(report: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """返回全部治理违例，避免开发者逐次修复才能看见下一项。"""
    errors: list[str] = []
    if policy.get("spec_version") != POLICY_VERSION:
        return ["可读性策略版本不受支持"]
    if report["thresholds"] != policy.get("thresholds"):
        errors.append("指标阈值与已接受策略不一致")
    if policy.get("required_module_docstrings") and report["missing_module_docstrings"]:
        errors.append("存在无模块说明的源码文件: " + ", ".join(report["missing_module_docstrings"]))
    if policy.get("required_public_behavior_docstrings") and report["undocumented_public_behavior"]:
        errors.append(
            "存在无语义说明的公共行为类或函数: " + ", ".join(report["undocumented_public_behavior"])
        )
    if (
        policy.get("required_high_risk_callable_docstrings")
        and report["undocumented_high_risk_callables"]
    ):
        errors.append(
            "存在无失败/恢复语义说明的高风险入口: "
            + ", ".join(report["undocumented_high_risk_callables"])
        )

    approved_files = policy.get("approved_oversized_files", {})
    for item in report["oversized_files"]:
        limit = approved_files.get(item["path"])
        if limit is None:
            errors.append(f"新增超大文件: {item['path']} ({item['lines']}行)")
        elif item["lines"] > limit:
            errors.append(f"超大文件继续增长: {item['path']} ({item['lines']}>{limit})")

    approved_symbols = policy.get("approved_oversized_symbols", {})
    for item in report["oversized_symbols"]:
        limit = approved_symbols.get(item["id"])
        if limit is None:
            errors.append(f"新增超大或高复杂度符号: {item['id']}")
            continue
        if item["lines"] > limit["max_lines"]:
            errors.append(f"既有热点长度增长: {item['id']} ({item['lines']}>{limit['max_lines']})")
        complexity = item.get("decision_complexity", 0)
        if complexity > limit["max_decision_complexity"]:
            errors.append(
                f"既有热点复杂度增长: {item['id']} "
                f"({complexity}>{limit['max_decision_complexity']})"
            )

    approved_edges = set(policy.get("approved_package_dependencies", ()))
    new_edges = sorted(set(report["package_dependencies"]) - approved_edges)
    if new_edges:
        errors.append("新增一级包依赖边: " + ", ".join(new_edges))
    approved_cycles = {tuple(cycle) for cycle in policy.get("approved_dependency_cycles", ())}
    new_cycles = sorted(
        tuple(cycle) for cycle in report["dependency_cycles"] if tuple(cycle) not in approved_cycles
    )
    if new_cycles:
        errors.append("新增一级包依赖环: " + ", ".join("/".join(cycle) for cycle in new_cycles))
    if report["public_api"] != policy.get("public_api"):
        errors.append("公共 __all__ 快照变化；必须经独立契约评审并更新策略")
    return errors


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--revision")
    parser.add_argument("--write-report", type=Path)
    parser.add_argument("--write-policy", type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-final-report", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> None:
    arguments = _parse_args()
    report = build_report(arguments.source_root, revision=arguments.revision)
    if arguments.write_report is not None:
        _write_json(arguments.write_report, report)
    if arguments.write_policy is not None:
        _write_json(arguments.write_policy, build_policy(report))

    errors: list[str] = []
    if arguments.check:
        policy = json.loads(arguments.policy.read_text(encoding="utf-8"))
        errors.extend(check_report(report, policy))
    if arguments.check_final_report:
        expected = json.loads(DEFAULT_FINAL_REPORT.read_text(encoding="utf-8"))
        if report != expected:
            errors.append(f"最终可读性报告已漂移: {DEFAULT_FINAL_REPORT}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)
    if not arguments.quiet:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
