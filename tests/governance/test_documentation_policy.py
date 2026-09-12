from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]


def _documentation_module() -> ModuleType:
    path = ROOT / "scripts" / "documentation_check.py"
    spec = importlib.util.spec_from_file_location("harnessix_documentation_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _policy(module: ModuleType) -> object:
    return module.load_policy(ROOT)


def _frontmatter(
    *,
    doc_type: str = "module-design",
    status: str = "reviewing",
    version: str = "1",
    revision: str = "pending",
    extra: str = "",
) -> str:
    return f"""---
doc_type: {doc_type}
status: {status}
version: {version}
code_revision: {revision}
owners:
  - core
modules:
  - sample
related_adrs: []
related_tests: []
supersedes: []
{extra}---
"""


def _write_document(
    module: ModuleType,
    root: Path,
    relative: str,
    body: str,
    *,
    frontmatter: str | None = None,
) -> tuple[object, list[object]]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((frontmatter or _frontmatter()) + body, encoding="utf-8")
    return module._read_document(root, path, _policy(module))


def _finding_codes(findings: list[object] | tuple[object, ...]) -> set[str]:
    return {finding.code for finding in findings}


def test_current_repository_satisfies_documentation_policy() -> None:
    module = _documentation_module()
    report = module.check_repository(ROOT, _policy(module))

    assert report.findings == ()
    assert report.document_count > 0
    assert report.link_count > 0
    assert report.mermaid_count > 0
    assert report.source_package_count == len(module._source_packages(ROOT))


def test_policy_loader_rejects_duplicate_and_weakened_v1_contract(tmp_path: Path) -> None:
    module = _documentation_module()
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"harnessix.documentation-policy/v1",'
        '"schema_version":"harnessix.documentation-policy/v1"}',
        encoding="utf-8",
    )
    with pytest.raises(module.PolicyError, match="重复键"):
        module.load_policy(tmp_path, duplicate.name)

    policy = json.loads((ROOT / module.DEFAULT_POLICY).read_text(encoding="utf-8"))
    policy["required_metadata"].remove("related_tests")
    weakened = tmp_path / "weakened.json"
    weakened.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(module.PolicyError, match="必填元数据"):
        module.load_policy(tmp_path, weakened.name)


def test_yaml_parser_rejects_duplicate_keys_aliases_and_non_object() -> None:
    module = _documentation_module()

    _, duplicate = module._parse_metadata("docs/a.md", "status: draft\nstatus: current\n")
    _, alias = module._parse_metadata("docs/a.md", "base: &base value\ncopy: *base\n")
    _, non_object = module._parse_metadata("docs/a.md", "- value\n")

    assert _finding_codes(duplicate) == {"metadata_yaml_invalid"}
    assert _finding_codes(alias) == {"metadata_alias_forbidden"}
    assert _finding_codes(non_object) == {"metadata_object_required"}


def test_metadata_validator_aggregates_schema_and_lifecycle_failures(tmp_path: Path) -> None:
    module = _documentation_module()
    frontmatter = _frontmatter(
        status="ready",
        version="0",
        revision="not-a-revision",
        extra="unknown_field: value\n",
    )
    document, parse_findings = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        "# Sample\n",
        frontmatter=frontmatter,
    )
    assert document is not None
    assert parse_findings == []

    findings = module.validate_metadata(
        tmp_path,
        {document.path: document},
        _policy(module),
        verify_git_revisions=False,
    )

    assert {
        "metadata_field_unknown",
        "metadata_status_invalid",
        "metadata_version_invalid",
        "metadata_revision_invalid",
    } <= _finding_codes(findings)

    pending, _ = _write_document(
        module,
        tmp_path,
        "docs/pending.md",
        "# Pending\n",
        frontmatter=_frontmatter(status="current"),
    )
    assert pending is not None
    pending_findings = module.validate_metadata(
        tmp_path,
        {pending.path: pending},
        _policy(module),
        verify_git_revisions=False,
    )
    assert "metadata_pending_forbidden" in _finding_codes(pending_findings)


def test_metadata_validator_rejects_unresolvable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _documentation_module()
    document, _ = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        "# Sample\n",
        frontmatter=_frontmatter(revision="a" * 40),
    )
    assert document is not None
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=f"{'a' * 40}^{{commit}} missing\n", stderr=""
        ),
    )

    findings = module.validate_metadata(
        tmp_path,
        {document.path: document},
        _policy(module),
        verify_git_revisions=True,
    )

    assert _finding_codes(findings) == {"metadata_revision_unresolvable"}


def test_metadata_validator_rejects_unsafe_and_mistyped_relations(tmp_path: Path) -> None:
    module = _documentation_module()
    frontmatter = _frontmatter().replace(
        "related_tests: []",
        "related_tests:\n  - docs/missing.md\n  - ../outside.py",
    )
    document, _ = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        "# Sample\n",
        frontmatter=frontmatter,
    )
    assert document is not None

    findings = module.validate_metadata(
        tmp_path,
        {document.path: document},
        _policy(module),
        verify_git_revisions=False,
    )

    assert {
        "metadata_target_missing",
        "metadata_target_unsafe",
        "metadata_test_target_invalid",
    } <= _finding_codes(findings)


def test_markdown_links_validate_target_anchor_protocol_and_boundary(tmp_path: Path) -> None:
    module = _documentation_module()
    target, _ = _write_document(module, tmp_path, "docs/target.md", "# Existing heading\n")
    source, parse_findings = _write_document(
        module,
        tmp_path,
        "docs/source.md",
        """# Source

[missing](missing.md)
[bad anchor](target.md#not-there)
[escape](../../outside.md)
[protocol](file:///etc/passwd)
[malformed](target.md%GG)
""",
    )
    assert target is not None and source is not None
    assert parse_findings == []

    findings = module.validate_links(
        tmp_path,
        {source.path: source, target.path: target},
        _policy(module),
    )

    assert _finding_codes(findings) == {
        "link_target_invalid",
        "link_target_missing",
        "link_anchor_missing",
    }


def test_required_sections_only_accept_markdown_headings(tmp_path: Path) -> None:
    module = _documentation_module()
    document, _ = _write_document(
        module,
        tmp_path,
        "docs/modules/sample.md",
        """# Sample

正文提到需求背景、设计目标、模块上下文、流程、接口、失败恢复、安全、测试、源码映射和风险。
""",
    )
    assert document is not None

    findings = module.validate_sections({document.path: document}, _policy(module))

    assert len(findings) == len(_policy(module).required_sections["module-design"])
    assert _finding_codes(findings) == {"section_required_missing"}


def test_repository_relations_detect_index_and_module_traceability_gaps(
    tmp_path: Path,
) -> None:
    module = _documentation_module()
    (tmp_path / "src/harnessix/sample").mkdir(parents=True)
    (tmp_path / "src/harnessix/sample/__init__.py").write_text("", encoding="utf-8")
    adr, _ = _write_document(
        module,
        tmp_path,
        "docs/adr/0001-sample.md",
        "# ADR\n",
        frontmatter=_frontmatter(doc_type="adr"),
    )
    research, _ = _write_document(
        module,
        tmp_path,
        "docs/research/sample.md",
        "# Research\n",
        frontmatter=_frontmatter(doc_type="source-research"),
    )
    adr_index, _ = _write_document(module, tmp_path, "docs/adr/README.md", "# ADR index\n")
    research_index, _ = _write_document(
        module, tmp_path, "docs/research/README.md", "# Research index\n"
    )
    module_doc, _ = _write_document(
        module,
        tmp_path,
        "docs/modules/sample.md",
        "# Sample module\n",
        frontmatter=_frontmatter(status="current"),
    )
    documents = {
        document.path: document
        for document in (adr, research, adr_index, research_index, module_doc)
        if document is not None
    }
    policy = replace(_policy(module), root_source_documents={})

    findings, package_count = module.validate_repository_relations(tmp_path, documents, policy)

    assert package_count == 1
    assert {
        "index_adr_missing",
        "index_research_missing",
        "module_source_link_missing",
        "module_test_link_missing",
    } <= _finding_codes(findings)


def test_sensitive_content_findings_never_echo_matched_values(tmp_path: Path) -> None:
    module = _documentation_module()
    secret = "sk-" + "A" * 24
    _, findings = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        f"# Sample\n\n/Users/example/private\n{secret}\n用户要求继续。\n",
    )

    assert {
        "content_personal_path",
        "content_possible_secret",
        "content_dialogue_process",
    } <= _finding_codes(findings)
    assert secret not in "\n".join(finding.message for finding in findings)


def test_document_loader_enforces_size_boundary(tmp_path: Path) -> None:
    module = _documentation_module()
    policy = _policy(module)
    limits = dict(policy.limits)
    limits["max_document_bytes"] = 8
    bounded = replace(policy, limits=limits)
    path = tmp_path / "docs/large.md"
    path.parent.mkdir(parents=True)
    path.write_text("0123456789", encoding="utf-8")

    document, findings = module._read_document(tmp_path, path, bounded)

    assert document is None
    assert _finding_codes(findings) == {"document_size_limit"}


def test_document_parser_reports_unclosed_fence(tmp_path: Path) -> None:
    module = _documentation_module()

    document, findings = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        "# Sample\n\n```mermaid\nflowchart LR\nA --> B\n",
    )

    assert document is not None
    assert _finding_codes(findings) == {"document_fence_unclosed"}


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (
            {"src/harnessix/agent/runtime.py"},
            {"doc_sync_module_required"},
        ),
        (
            {"src/harnessix/agent_cli.py"},
            {"doc_sync_root_required", "doc_sync_change_design_required"},
        ),
        (
            {
                "src/harnessix/alpha/runtime.py",
                "src/harnessix/beta/runtime.py",
                "docs/modules/alpha.md",
                "docs/modules/beta.md",
            },
            {"doc_sync_change_design_required"},
        ),
    ],
)
def test_changed_documentation_requires_matching_designs(
    changed: set[str], expected: set[str]
) -> None:
    module = _documentation_module()

    findings = module.validate_changed_documentation(changed, {}, _policy(module))

    assert _finding_codes(findings) == expected


def test_changed_documentation_accepts_module_and_change_design(tmp_path: Path) -> None:
    module = _documentation_module()
    change_path = "docs/changes/sample.md"
    change = module.Document(
        path=change_path,
        absolute_path=tmp_path / change_path,
        metadata={"doc_type": "change-design"},
        body="",
        headings=(),
        links=(),
        mermaid_blocks=(),
    )

    findings = module.validate_changed_documentation(
        {
            "src/harnessix/execution/runtime.py",
            "docs/modules/execution.md",
            change_path,
        },
        {change_path: change},
        _policy(module),
    )

    assert findings == []


def test_changed_path_collection_aggregates_all_git_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _documentation_module()
    responses = iter(
        [
            ({"committed.py"}, None),
            ({"staged.py"}, None),
            ({"worktree.py"}, None),
            ({"untracked.py"}, None),
        ]
    )
    monkeypatch.setattr(module, "_git_paths", lambda *_: next(responses))

    paths, findings = module.collect_changed_paths(ROOT, "a" * 40, _policy(module))

    assert paths == {"committed.py", "staged.py", "worktree.py", "untracked.py"}
    assert findings == []


def test_changed_path_collection_fails_closed_for_invalid_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _documentation_module()
    failure = module.Finding("git_diff_failed", ".", "failed")
    monkeypatch.setattr(module, "_git_paths", lambda *_: (set(), failure))

    paths, findings = module.collect_changed_paths(ROOT, "b" * 40, _policy(module))

    assert paths is None
    assert _finding_codes(findings) == {"git_base_invalid"}


def test_mermaid_structure_and_renderer_failure_are_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _documentation_module()
    document, _ = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        "# Sample\n\n```mermaid\nunknownDiagram\n```\n\n```mermaid\n```\n",
    )
    assert document is not None
    documents = {document.path: document}
    assert _finding_codes(module.validate_mermaid_structure(documents, _policy(module))) == {
        "mermaid_block_empty",
        "mermaid_type_invalid",
    }

    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/mmdc")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    findings = module.render_mermaid(documents, _policy(module), changed_paths=None)
    assert _finding_codes(findings) == {"mermaid_render_failed"}


def test_mermaid_renderer_requires_binary_and_accepts_nonempty_svg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _documentation_module()
    document, _ = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        "# Sample\n\n```mermaid\nflowchart LR\nA --> B\n```\n",
    )
    assert document is not None
    documents = {document.path: document}
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    missing = module.render_mermaid(documents, _policy(module), changed_paths=None)
    assert _finding_codes(missing) == {"mermaid_renderer_missing"}

    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/mmdc")

    def render(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        output = Path(command[command.index("-o") + 1])
        output.write_text("<svg/>", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", render)
    assert module.render_mermaid(documents, _policy(module), changed_paths=None) == []


def test_mermaid_renderer_timeout_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _documentation_module()
    document, _ = _write_document(
        module,
        tmp_path,
        "docs/sample.md",
        "# Sample\n\n```mermaid\nflowchart LR\nA --> B\n```\n",
    )
    assert document is not None
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/mmdc")

    def timeout(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired("mmdc", 1)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    findings = module.render_mermaid({document.path: document}, _policy(module), changed_paths=None)
    assert _finding_codes(findings) == {"mermaid_render_timeout"}


def test_cli_text_and_json_reports_have_stable_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _documentation_module()
    policy = _policy(module)
    success = module.CheckReport(policy.schema_version, 1, 2, 3, 4, 5, ())
    monkeypatch.setattr(module, "load_policy", lambda *_: policy)
    monkeypatch.setattr(module, "check_repository", lambda *_, **__: success)

    assert module.main(["--root", str(ROOT)]) == 0
    assert "文档门禁通过" in capsys.readouterr().out

    failure = replace(success, findings=(module.Finding("sample_error", "docs/a.md", "失败"),))
    monkeypatch.setattr(module, "check_repository", lambda *_, **__: failure)
    assert module.main(["--root", str(ROOT), "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy_version"] == policy.schema_version
    assert payload["findings"][0]["code"] == "sample_error"
