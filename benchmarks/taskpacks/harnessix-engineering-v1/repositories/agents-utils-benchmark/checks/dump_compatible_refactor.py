import ast
from pathlib import Path

from src.agent_utils import to_dump_compatible

source = Path("src/agent_utils.py").read_text(encoding="utf-8")
tree = ast.parse(source)
function = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "to_dump_compatible"
)
separate_sequence_checks = 0
combined_sequence_check = False
for node in ast.walk(function):
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id != "isinstance"
    ):
        continue
    if len(node.args) != 2:
        continue
    target = node.args[1]
    if isinstance(target, ast.Name) and target.id in {"list", "tuple"}:
        separate_sequence_checks += 1
    if isinstance(target, ast.Tuple):
        names = {item.id for item in target.elts if isinstance(item, ast.Name)}
        combined_sequence_check = combined_sequence_check or {"list", "tuple"} <= names

assert separate_sequence_checks == 0
assert combined_sequence_check
assert to_dump_compatible({"items": (1, [2, 3])}) == {"items": [1, [2, 3]]}
assert to_dump_compatible("text") == "text"
