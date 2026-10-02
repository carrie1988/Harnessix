"""只验证本切片文档，复用正式门禁，不把仓库根加入导入路径。"""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[3]
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != ROOT]
package = types.ModuleType("scripts")
package.__path__ = [str(ROOT / "scripts")]
sys.modules["scripts"] = package
spec = importlib.util.spec_from_file_location("scripts.documentation_check", ROOT / "scripts/documentation_check.py")
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)
policy = checker.load_policy(ROOT)
paths = [ROOT / "docs/changes/m09-r3-eval-publication-wiring.md",
         Path(__file__).with_name("REPORT.md"), Path(__file__).with_name("REVIEW.md")]
documents, findings = {}, []
for path in paths:
    document, errors = checker._read_document(ROOT, path, policy)
    findings.extend(errors)
    if document is not None:
        documents[document.path] = document
findings.extend(checker.validate_metadata(ROOT, documents, policy, verify_git_revisions=True))
findings.extend(checker.validate_links(ROOT, documents, policy))
findings.extend(checker.validate_sections(documents, policy))
findings.extend(checker.validate_mermaid_structure(documents, policy))
print(json.dumps({"status": "PASS" if not findings else "FAIL", "documents": list(documents),
    "scope": "owned documents only; no repository-relation or changed-module gate claimed",
    "findings": [asdict(f) for f in findings]}, ensure_ascii=False, indent=2))
raise SystemExit(bool(findings))
