"""执行Harnessix文档元数据、追踪关系和变更同步门禁。"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken

DEFAULT_POLICY = "governance/documentation-policy-v1.json"
POLICY_VERSION = "harnessix.documentation-policy/v1"
DOCUMENT_TYPES = frozenset(
    {
        "product-charter",
        "roadmap",
        "system-architecture",
        "module-design",
        "change-design",
        "contract",
        "adr",
        "source-research",
        "test-and-eval-design",
        "validation-evidence",
        "deployment-design",
        "threat-model",
        "governance",
        "governance-index",
        "documentation-standard",
        "source-reading-guide",
        "template",
    }
)
DOCUMENT_STATUSES = frozenset(
    {"draft", "reviewing", "current", "historical", "superseded", "deprecated"}
)
METADATA_FIELDS = frozenset(
    {
        "doc_type",
        "status",
        "version",
        "code_revision",
        "owners",
        "modules",
        "related_adrs",
        "related_tests",
        "supersedes",
    }
)
ZERO_REVISION = re.compile(r"^0+$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})([^`]*)$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
MANUAL_ANCHOR = re.compile(r"<a\s+(?:name|id)=[\"']([^\"']+)[\"']", re.IGNORECASE)
PERCENT_ESCAPE_ERROR = re.compile(r"%(?![0-9A-Fa-f]{2})")
NUMBERED_ADR = re.compile(r"^docs/adr/[0-9]{4}-.+\.md$")


class DuplicateKeySafeLoader(yaml.SafeLoader):
    """拒绝YAML重复键，避免后写字段静默覆盖安全元数据。"""


def _construct_unique_mapping(
    loader: DuplicateKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class Finding:
    """单项可机器消费的文档门禁错误。"""

    code: str
    path: str
    message: str
    line: int | None = None

    def sort_key(self) -> tuple[str, int, str, str]:
        return (self.path, self.line or 0, self.code, self.message)


@dataclass(frozen=True)
class HeadingRecord:
    text: str
    anchor: str
    line: int


@dataclass(frozen=True)
class LinkRecord:
    destination: str
    line: int


@dataclass(frozen=True)
class MermaidBlock:
    source: str
    line: int


@dataclass(frozen=True)
class Document:
    path: str
    absolute_path: Path
    metadata: dict[str, object] | None
    body: str
    headings: tuple[HeadingRecord, ...]
    links: tuple[LinkRecord, ...]
    mermaid_blocks: tuple[MermaidBlock, ...]


@dataclass(frozen=True)
class DocumentationPolicy:
    schema_version: str
    allowed_doc_types: frozenset[str]
    allowed_statuses: frozenset[str]
    required_metadata: frozenset[str]
    limits: dict[str, int]
    required_sections: dict[str, tuple[tuple[str, ...], ...]]
    doc_type_roots: dict[str, str]
    root_source_documents: dict[str, tuple[str, ...]]
    major_change_patterns: tuple[re.Pattern[str], ...]
    allowed_mermaid_diagram_types: frozenset[str]
    forbidden_patterns: tuple[tuple[str, re.Pattern[str], str], ...]


@dataclass(frozen=True)
class CheckReport:
    policy_version: str
    document_count: int
    link_count: int
    mermaid_count: int
    source_package_count: int
    changed_path_count: int
    findings: tuple[Finding, ...]


class PolicyError(ValueError):
    """表示策略文件无法形成安全、确定的检查合同。"""


def _configure_utf8_console() -> None:
    """统一Windows和POSIX CLI编码，避免中文诊断在旧代码页写出失败。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"策略JSON包含重复键：{key}")
        result[key] = value
    return result


def _string_list(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PolicyError(f"{field}必须是{'可空' if allow_empty else '非空'}字符串数组")
    if not all(isinstance(item, str) and item for item in value):
        raise PolicyError(f"{field}只能包含非空字符串")
    if len(value) != len(set(value)):
        raise PolicyError(f"{field}不能包含重复值")
    return tuple(value)


def load_policy(root: Path, policy_path: str = DEFAULT_POLICY) -> DocumentationPolicy:
    """从仓库内加载并严格验证版本化文档策略。"""

    target = (root / policy_path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise PolicyError("策略路径必须位于仓库内") from exc
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"无法读取策略：{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise PolicyError("策略根必须是JSON对象")

    required_keys = {
        "schema_version",
        "allowed_doc_types",
        "allowed_statuses",
        "required_metadata",
        "limits",
        "required_sections",
        "doc_type_roots",
        "root_source_documents",
        "major_change_patterns",
        "allowed_mermaid_diagram_types",
        "forbidden_patterns",
    }
    if set(data) != required_keys:
        raise PolicyError(f"策略字段不匹配，差异：{sorted(set(data) ^ required_keys)}")
    if data["schema_version"] != POLICY_VERSION:
        raise PolicyError(f"不支持的策略版本：{data['schema_version']!r}")

    limits_raw = data["limits"]
    required_limits = {
        "max_documents",
        "max_document_bytes",
        "max_frontmatter_bytes",
        "max_headings_per_document",
        "max_links_per_document",
        "max_mermaid_blocks_per_document",
        "max_mermaid_block_bytes",
        "git_timeout_seconds",
        "mermaid_timeout_seconds",
    }
    if not isinstance(limits_raw, dict) or set(limits_raw) != required_limits:
        raise PolicyError("limits字段不完整或包含未知项")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in limits_raw.values()
    ):
        raise PolicyError("limits必须全部为正整数")

    sections_raw = data["required_sections"]
    if not isinstance(sections_raw, dict):
        raise PolicyError("required_sections必须是对象")
    sections: dict[str, tuple[tuple[str, ...], ...]] = {}
    for doc_type, groups in sections_raw.items():
        if not isinstance(doc_type, str) or not isinstance(groups, list) or not groups:
            raise PolicyError("required_sections条目无效")
        sections[doc_type] = tuple(
            _string_list(group, f"required_sections.{doc_type}") for group in groups
        )

    roots_raw = data["doc_type_roots"]
    if not isinstance(roots_raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in roots_raw.items()
    ):
        raise PolicyError("doc_type_roots必须是字符串映射")

    root_docs_raw = data["root_source_documents"]
    if not isinstance(root_docs_raw, dict):
        raise PolicyError("root_source_documents必须是对象")
    root_docs = {
        source: _string_list(targets, f"root_source_documents.{source}")
        for source, targets in root_docs_raw.items()
        if isinstance(source, str) and source
    }
    if len(root_docs) != len(root_docs_raw):
        raise PolicyError("root_source_documents包含非法源码路径")

    try:
        major_patterns = tuple(
            re.compile(pattern)
            for pattern in _string_list(data["major_change_patterns"], "major_change_patterns")
        )
    except re.error as exc:
        raise PolicyError(f"重大变更正则无效：{exc}") from exc

    forbidden_raw = data["forbidden_patterns"]
    if not isinstance(forbidden_raw, list) or not forbidden_raw:
        raise PolicyError("forbidden_patterns必须是非空数组")
    forbidden: list[tuple[str, re.Pattern[str], str]] = []
    codes: set[str] = set()
    for item in forbidden_raw:
        if not isinstance(item, dict) or set(item) != {"code", "pattern", "message"}:
            raise PolicyError("forbidden_patterns条目字段无效")
        if not all(isinstance(item[key], str) and item[key] for key in item):
            raise PolicyError("forbidden_patterns条目必须是非空字符串")
        if item["code"] in codes:
            raise PolicyError(f"重复的内容规则：{item['code']}")
        codes.add(item["code"])
        try:
            forbidden.append((item["code"], re.compile(item["pattern"]), item["message"]))
        except re.error as exc:
            raise PolicyError(f"内容规则正则无效：{item['code']}") from exc

    allowed_types = frozenset(_string_list(data["allowed_doc_types"], "allowed_doc_types"))
    allowed_statuses = frozenset(_string_list(data["allowed_statuses"], "allowed_statuses"))
    required_metadata = frozenset(_string_list(data["required_metadata"], "required_metadata"))
    if allowed_types != DOCUMENT_TYPES:
        raise PolicyError("v1文档类型必须与文档工程规范完全一致")
    if allowed_statuses != DOCUMENT_STATUSES:
        raise PolicyError("v1文档状态必须与文档工程规范完全一致")
    if required_metadata != METADATA_FIELDS:
        raise PolicyError("v1必填元数据必须与文档工程规范完全一致")
    unknown_section_types = set(sections) - allowed_types
    if unknown_section_types:
        raise PolicyError(f"章节规则引用未知文档类型：{sorted(unknown_section_types)}")

    return DocumentationPolicy(
        schema_version=POLICY_VERSION,
        allowed_doc_types=allowed_types,
        allowed_statuses=allowed_statuses,
        required_metadata=required_metadata,
        limits={key: int(value) for key, value in limits_raw.items()},
        required_sections=sections,
        doc_type_roots=dict(roots_raw),
        root_source_documents=root_docs,
        major_change_patterns=major_patterns,
        allowed_mermaid_diagram_types=frozenset(
            _string_list(
                data["allowed_mermaid_diagram_types"],
                "allowed_mermaid_diagram_types",
            )
        ),
        forbidden_patterns=tuple(forbidden),
    )


def _repository_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _github_slug(text: str) -> str:
    value = html.unescape(re.sub(r"<[^>]*>", "", text))
    value = re.sub(r"[`*_~]", "", value).casefold()
    return "".join(
        "-" if char.isspace() else char
        for char in value
        if char in "-_" or char.isspace() or unicodedata.category(char)[0] in "LMN"
    )


def _parse_markdown(
    path: str,
    body: str,
    body_start_line: int,
    policy: DocumentationPolicy,
) -> tuple[
    tuple[HeadingRecord, ...],
    tuple[LinkRecord, ...],
    tuple[MermaidBlock, ...],
    list[Finding],
]:
    findings: list[Finding] = []
    headings: list[HeadingRecord] = []
    links: list[LinkRecord] = []
    mermaid: list[MermaidBlock] = []
    anchor_counts: dict[str, int] = {}
    fence_char: str | None = None
    fence_length = 0
    fence_language = ""
    fence_line = 0
    fence_content: list[str] = []

    for index, line in enumerate(body.splitlines(), start=body_start_line):
        fence_match = FENCE.match(line)
        if fence_char is not None:
            if (
                fence_match
                and fence_match.group(1)[0] == fence_char
                and len(fence_match.group(1)) >= fence_length
            ):
                if fence_language == "mermaid":
                    source = "\n".join(fence_content).strip()
                    mermaid.append(MermaidBlock(source=source, line=fence_line))
                fence_char = None
                fence_content = []
                continue
            fence_content.append(line)
            continue
        if fence_match:
            marker = fence_match.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
            fence_language = (
                fence_match.group(2).strip().split(maxsplit=1)[0].casefold()
                if fence_match.group(2).strip()
                else ""
            )
            fence_line = index
            continue

        heading_match = HEADING.match(line)
        if heading_match:
            text = heading_match.group(1).strip()
            base = _github_slug(text)
            if base:
                duplicate = anchor_counts.get(base, 0)
                anchor_counts[base] = duplicate + 1
                anchor = base if duplicate == 0 else f"{base}-{duplicate}"
                headings.append(HeadingRecord(text=text, anchor=anchor, line=index))
        for manual in MANUAL_ANCHOR.finditer(line):
            headings.append(HeadingRecord(text=manual.group(1), anchor=manual.group(1), line=index))
        for match in MARKDOWN_LINK.finditer(line):
            destination = _link_destination(match.group(1).strip())
            if destination:
                links.append(LinkRecord(destination=destination, line=index))

    if fence_char is not None:
        findings.append(Finding("document_fence_unclosed", path, "围栏代码块未闭合", fence_line))
    if len(headings) > policy.limits["max_headings_per_document"]:
        findings.append(Finding("document_heading_limit", path, "标题数量超过策略上限"))
    if len(links) > policy.limits["max_links_per_document"]:
        findings.append(Finding("document_link_limit", path, "链接数量超过策略上限"))
    if len(mermaid) > policy.limits["max_mermaid_blocks_per_document"]:
        findings.append(Finding("mermaid_block_limit", path, "Mermaid图块数量超过策略上限"))
    return tuple(headings), tuple(links), tuple(mermaid), findings


def _link_destination(raw: str) -> str:
    if raw.startswith("<") and raw.endswith(">"):
        return raw[1:-1]
    titled = re.split(r"\s+(?=[\"'])", raw, maxsplit=1)
    return titled[0]


def _read_document(
    root: Path,
    path: Path,
    policy: DocumentationPolicy,
) -> tuple[Document | None, list[Finding]]:
    relative = _repository_path(root, path)
    findings: list[Finding] = []
    if path.is_symlink():
        return None, [Finding("document_symlink_forbidden", relative, "Markdown文档不能是符号链接")]
    try:
        size = path.stat().st_size
        if size > policy.limits["max_document_bytes"]:
            return None, [Finding("document_size_limit", relative, "Markdown文档超过字节上限")]
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError) as exc:
        return None, [
            Finding("document_read_failed", relative, f"无法以UTF-8读取文档：{type(exc).__name__}")
        ]

    lines = text.splitlines(keepends=True)
    metadata: dict[str, object] | None = None
    body = text
    body_start_line = 1
    if not lines or lines[0].strip() != "---":
        findings.append(
            Finding("metadata_frontmatter_missing", relative, "文档必须以YAML头开始", 1)
        )
    else:
        closing = next(
            (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            None,
        )
        if closing is None:
            findings.append(Finding("metadata_frontmatter_unclosed", relative, "YAML头未闭合", 1))
        else:
            frontmatter = "".join(lines[1:closing])
            body = "".join(lines[closing + 1 :])
            body_start_line = closing + 2
            if len(frontmatter.encode("utf-8")) > policy.limits["max_frontmatter_bytes"]:
                findings.append(Finding("metadata_size_limit", relative, "YAML头超过字节上限", 1))
            else:
                metadata, metadata_findings = _parse_metadata(relative, frontmatter)
                findings.extend(metadata_findings)

    headings, links, mermaid, markdown_findings = _parse_markdown(
        relative,
        body,
        body_start_line,
        policy,
    )
    findings.extend(markdown_findings)
    for code, pattern, message in policy.forbidden_patterns:
        match = pattern.search(text)
        if match:
            findings.append(Finding(code, relative, message, _line_number(text, match.start())))
    return (
        Document(
            path=relative,
            absolute_path=path,
            metadata=metadata,
            body=body,
            headings=headings,
            links=links,
            mermaid_blocks=mermaid,
        ),
        findings,
    )


def _parse_metadata(
    path: str,
    frontmatter: str,
) -> tuple[dict[str, object] | None, list[Finding]]:
    try:
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(frontmatter)):
            return None, [
                Finding("metadata_alias_forbidden", path, "YAML头不能使用Anchor或Alias", 1)
            ]
        loaded = yaml.load(frontmatter, Loader=DuplicateKeySafeLoader)
    except (yaml.YAMLError, ConstructorError) as exc:
        return None, [
            Finding("metadata_yaml_invalid", path, f"YAML头无效：{type(exc).__name__}", 1)
        ]
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        return None, [Finding("metadata_object_required", path, "YAML头必须是字符串键对象", 1)]
    return loaded, []


def load_documents(
    root: Path,
    policy: DocumentationPolicy,
) -> tuple[dict[str, Document], list[Finding]]:
    """有界加载全部正式Markdown，并聚合单文档解析问题。"""

    docs_root = root / "docs"
    paths = sorted(docs_root.rglob("*.md")) if docs_root.is_dir() else []
    if len(paths) > policy.limits["max_documents"]:
        return {}, [Finding("document_count_limit", "docs", "Markdown文档数量超过策略上限")]
    documents: dict[str, Document] = {}
    findings: list[Finding] = []
    for path in paths:
        document, local_findings = _read_document(root, path, policy)
        findings.extend(local_findings)
        if document is not None:
            documents[document.path] = document
    return documents, findings


def _metadata_line(document: Document, key: str) -> int:
    for index, line in enumerate(
        document.absolute_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if line.startswith(f"{key}:"):
            return index
    return 1


def _safe_repository_target(root: Path, target: str) -> Path | None:
    if not target or "\\" in target:
        return None
    pure = PurePosixPath(target)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    resolved = (root / Path(*pure.parts)).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def validate_metadata(
    root: Path,
    documents: dict[str, Document],
    policy: DocumentationPolicy,
    *,
    verify_git_revisions: bool,
) -> list[Finding]:
    """验证YAML字段、生命周期、关系目标和Git版本。"""

    findings: list[Finding] = []
    revisions: dict[str, list[Document]] = {}
    for document in documents.values():
        metadata = document.metadata
        if metadata is None:
            continue
        keys = set(metadata)
        for missing in sorted(policy.required_metadata - keys):
            findings.append(
                Finding("metadata_field_missing", document.path, f"缺少字段：{missing}", 1)
            )
        for unknown in sorted(keys - policy.required_metadata):
            findings.append(
                Finding("metadata_field_unknown", document.path, f"未知字段：{unknown}", 1)
            )

        doc_type = metadata.get("doc_type")
        status = metadata.get("status")
        version = metadata.get("version")
        revision = metadata.get("code_revision")
        if not isinstance(doc_type, str) or doc_type not in policy.allowed_doc_types:
            findings.append(
                Finding(
                    "metadata_doc_type_invalid",
                    document.path,
                    "doc_type不在策略枚举中",
                    _metadata_line(document, "doc_type"),
                )
            )
        if not isinstance(status, str) or status not in policy.allowed_statuses:
            findings.append(
                Finding(
                    "metadata_status_invalid",
                    document.path,
                    "status不在策略枚举中",
                    _metadata_line(document, "status"),
                )
            )
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            findings.append(
                Finding(
                    "metadata_version_invalid",
                    document.path,
                    "version必须是正整数",
                    _metadata_line(document, "version"),
                )
            )
        if revision == "pending":
            if status not in {"draft", "reviewing"}:
                findings.append(
                    Finding(
                        "metadata_pending_forbidden",
                        document.path,
                        "只有draft/reviewing文档可使用pending版本",
                        _metadata_line(document, "code_revision"),
                    )
                )
        elif not isinstance(revision, str) or not REVISION.fullmatch(revision):
            findings.append(
                Finding(
                    "metadata_revision_invalid",
                    document.path,
                    "code_revision必须是40位小写提交或pending",
                    _metadata_line(document, "code_revision"),
                )
            )
        else:
            revisions.setdefault(revision, []).append(document)

        for field in ("owners", "modules"):
            value = metadata.get(field)
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(item, str) and item for item in value)
                or len(value) != len(set(value))
            ):
                findings.append(
                    Finding(
                        "metadata_list_invalid",
                        document.path,
                        f"{field}必须是非空唯一字符串数组",
                        _metadata_line(document, field),
                    )
                )
        for field in ("related_adrs", "related_tests", "supersedes"):
            value = metadata.get(field)
            if (
                not isinstance(value, list)
                or not all(isinstance(item, str) and item for item in value)
                or len(value) != len(set(value))
            ):
                findings.append(
                    Finding(
                        "metadata_list_invalid",
                        document.path,
                        f"{field}必须是唯一字符串数组",
                        _metadata_line(document, field),
                    )
                )
                continue
            for target in value:
                resolved = _safe_repository_target(root, target)
                if resolved is None:
                    findings.append(
                        Finding(
                            "metadata_target_unsafe",
                            document.path,
                            f"{field}包含不安全路径",
                            _metadata_line(document, field),
                        )
                    )
                    continue
                if not resolved.exists():
                    findings.append(
                        Finding(
                            "metadata_target_missing",
                            document.path,
                            f"{field}目标不存在：{target}",
                            _metadata_line(document, field),
                        )
                    )
                if field == "related_adrs" and not NUMBERED_ADR.fullmatch(target):
                    findings.append(
                        Finding(
                            "metadata_adr_target_invalid",
                            document.path,
                            "related_adrs必须指向编号ADR",
                            _metadata_line(document, field),
                        )
                    )
                if field == "related_tests" and not target.startswith("tests/"):
                    findings.append(
                        Finding(
                            "metadata_test_target_invalid",
                            document.path,
                            "related_tests必须位于tests/",
                            _metadata_line(document, field),
                        )
                    )
                if field == "supersedes" and not target.startswith("docs/"):
                    findings.append(
                        Finding(
                            "metadata_supersedes_target_invalid",
                            document.path,
                            "supersedes必须指向docs/",
                            _metadata_line(document, field),
                        )
                    )

        if isinstance(doc_type, str) and doc_type in policy.doc_type_roots:
            expected_root = policy.doc_type_roots[doc_type].rstrip("/") + "/"
            if not document.path.startswith(expected_root):
                findings.append(
                    Finding(
                        "metadata_doc_type_path_mismatch",
                        document.path,
                        f"{doc_type}文档必须位于{expected_root}",
                    )
                )

    if verify_git_revisions and revisions:
        findings.extend(_validate_revisions(root, revisions, policy))
    return findings


def _validate_revisions(
    root: Path,
    revisions: dict[str, list[Document]],
    policy: DocumentationPolicy,
) -> list[Finding]:
    ordered = sorted(revisions)
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objecttype)"],
            cwd=root,
            input="".join(f"{revision}^{{commit}}\n" for revision in ordered),
            text=True,
            capture_output=True,
            timeout=policy.limits["git_timeout_seconds"],
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [Finding("git_revision_check_failed", ".", "无法验证文档code_revision")]
    types = result.stdout.splitlines()
    findings: list[Finding] = []
    for revision, object_type in zip(ordered, types, strict=False):
        if object_type.strip() != "commit":
            for document in revisions[revision]:
                findings.append(
                    Finding(
                        "metadata_revision_unresolvable",
                        document.path,
                        "code_revision无法解析为Git提交",
                        _metadata_line(document, "code_revision"),
                    )
                )
    if result.returncode != 0 or len(types) != len(ordered):
        known = set(ordered[: len(types)])
        for revision in set(ordered) - known:
            for document in revisions[revision]:
                findings.append(
                    Finding(
                        "metadata_revision_unresolvable",
                        document.path,
                        "code_revision无法解析为Git提交",
                        _metadata_line(document, "code_revision"),
                    )
                )
    return findings


def _resolve_link(
    root: Path, document: Document, destination: str
) -> tuple[Path | None, str | None, str | None]:
    if PERCENT_ESCAPE_ERROR.search(destination):
        return None, None, "链接包含非法百分号编码"
    parsed = urlsplit(destination)
    if parsed.scheme:
        if parsed.scheme.casefold() not in {"http", "https", "mailto"}:
            return None, None, f"不允许的链接协议：{parsed.scheme}"
        return None, None, None
    if parsed.netloc:
        return None, None, "不允许省略协议的外部链接"
    decoded = unquote(parsed.path)
    if "\\" in decoded or Path(decoded).is_absolute():
        return None, None, "仓库链接必须使用相对POSIX路径"
    candidate = (
        document.absolute_path
        if not decoded
        else document.absolute_path.parent / Path(*PurePosixPath(decoded).parts)
    )
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None, None, "链接目标逃逸仓库根"
    return resolved, unquote(parsed.fragment), None


def _anchors_from_file(path: Path, policy: DocumentationPolicy) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError):
        return set()
    headings, _, _, _ = _parse_markdown(path.as_posix(), text, 1, policy)
    return {heading.anchor for heading in headings}


def validate_links(
    root: Path,
    documents: dict[str, Document],
    policy: DocumentationPolicy,
) -> list[Finding]:
    """验证Markdown相对目标、仓库边界和标题锚点。"""

    findings: list[Finding] = []
    anchor_cache = {
        document.absolute_path.resolve(): {heading.anchor for heading in document.headings}
        for document in documents.values()
    }
    for document in documents.values():
        for link in document.links:
            target, fragment, error = _resolve_link(root, document, link.destination)
            if error:
                findings.append(Finding("link_target_invalid", document.path, error, link.line))
                continue
            if target is None:
                continue
            if not target.exists():
                findings.append(
                    Finding(
                        "link_target_missing",
                        document.path,
                        f"链接目标不存在：{link.destination}",
                        link.line,
                    )
                )
                continue
            if fragment and target.is_file() and target.suffix.casefold() == ".md":
                anchors = anchor_cache.setdefault(target, _anchors_from_file(target, policy))
                if fragment.casefold() not in anchors:
                    findings.append(
                        Finding(
                            "link_anchor_missing",
                            document.path,
                            f"标题锚点不存在：#{fragment}",
                            link.line,
                        )
                    )
    return findings


def validate_sections(
    documents: dict[str, Document],
    policy: DocumentationPolicy,
) -> list[Finding]:
    """按标题而非正文关键词验证现行模块和新变更设计结构。"""

    findings: list[Finding] = []
    for document in documents.values():
        if document.metadata is None:
            continue
        doc_type = document.metadata.get("doc_type")
        if doc_type not in policy.required_sections:
            continue
        if doc_type == "change-design" and not document.path.startswith("docs/changes/"):
            continue
        headings = [heading.text.casefold() for heading in document.headings[1:]]
        for alternatives in policy.required_sections[doc_type]:
            if not any(
                keyword.casefold() in heading for heading in headings for keyword in alternatives
            ):
                findings.append(
                    Finding(
                        "section_required_missing",
                        document.path,
                        f"缺少语义章节（任一）：{' / '.join(alternatives)}",
                    )
                )
    return findings


def validate_mermaid_structure(
    documents: dict[str, Document],
    policy: DocumentationPolicy,
) -> list[Finding]:
    """离线验证Mermaid块预算和首个图类型声明。"""

    findings: list[Finding] = []
    for document in documents.values():
        for block in document.mermaid_blocks:
            if not block.source:
                findings.append(
                    Finding("mermaid_block_empty", document.path, "Mermaid图块不能为空", block.line)
                )
                continue
            if len(block.source.encode("utf-8")) > policy.limits["max_mermaid_block_bytes"]:
                findings.append(
                    Finding(
                        "mermaid_size_limit", document.path, "Mermaid图块超过字节上限", block.line
                    )
                )
                continue
            declaration = next(
                (
                    line.strip().split(maxsplit=1)[0]
                    for line in block.source.splitlines()
                    if line.strip() and not line.lstrip().startswith("%%")
                ),
                "",
            )
            if declaration not in policy.allowed_mermaid_diagram_types:
                findings.append(
                    Finding(
                        "mermaid_type_invalid",
                        document.path,
                        "Mermaid图块缺少允许的图类型声明",
                        block.line,
                    )
                )
    return findings


def _local_targets(root: Path, document: Document) -> set[str]:
    targets: set[str] = set()
    for link in document.links:
        target, _, error = _resolve_link(root, document, link.destination)
        if error or target is None or not target.exists():
            continue
        try:
            targets.add(_repository_path(root, target))
        except ValueError:
            continue
    return targets


def _source_packages(root: Path) -> list[str]:
    package_root = root / "src" / "harnessix"
    if not package_root.is_dir():
        return []
    return sorted(
        path.name
        for path in package_root.iterdir()
        if path.is_dir() and path.name != "__pycache__" and any(path.rglob("*.py"))
    )


def validate_repository_relations(
    root: Path,
    documents: dict[str, Document],
    policy: DocumentationPolicy,
) -> tuple[list[Finding], int]:
    """验证索引完整性、30个源码包和模块源码/测试链接。"""

    findings: list[Finding] = []
    index_rules = (
        ("docs/adr/README.md", NUMBERED_ADR, "index_adr_missing"),
        (
            "docs/research/README.md",
            re.compile(r"^docs/research/(?!README\.md).+\.md$"),
            "index_research_missing",
        ),
    )
    for index_path, member_pattern, code in index_rules:
        index = documents.get(index_path)
        if index is None:
            findings.append(
                Finding("index_document_missing", index_path, "索引文档不存在或无法解析")
            )
            continue
        targets = _local_targets(root, index)
        for member in sorted(path for path in documents if member_pattern.fullmatch(path)):
            if member not in targets:
                findings.append(Finding(code, index_path, f"索引未覆盖：{member}"))

    packages = _source_packages(root)
    for package in packages:
        module_path = f"docs/modules/{package.replace('_', '-')}.md"
        document = documents.get(module_path)
        if document is None:
            findings.append(
                Finding("module_document_missing", module_path, f"源码包{package}缺少现行模块设计")
            )
            continue
        metadata = document.metadata or {}
        if metadata.get("doc_type") != "module-design" or metadata.get("status") != "current":
            findings.append(
                Finding(
                    "module_document_not_current",
                    module_path,
                    "源码包模块设计必须是current module-design",
                )
            )
        modules = metadata.get("modules")
        if not isinstance(modules, list) or package not in modules:
            findings.append(
                Finding(
                    "module_metadata_package_missing",
                    module_path,
                    f"modules未声明源码包：{package}",
                )
            )
        targets = _local_targets(root, document)
        source_prefix = f"src/harnessix/{package}"
        if not any(
            target == source_prefix or target.startswith(source_prefix + "/") for target in targets
        ):
            findings.append(
                Finding("module_source_link_missing", module_path, f"缺少{source_prefix}源码链接")
            )
        if not any(target == "tests" or target.startswith("tests/") for target in targets):
            findings.append(Finding("module_test_link_missing", module_path, "缺少tests/测试链接"))

    for source, targets in policy.root_source_documents.items():
        if not (root / source).exists():
            findings.append(Finding("root_source_missing", source, "策略登记的根级源码不存在"))
        for target in targets:
            if target not in documents:
                findings.append(
                    Finding("root_source_document_missing", source, f"根级源码文档不存在：{target}")
                )
    return findings, len(packages)


def _git_paths(root: Path, args: list[str], timeout: int) -> tuple[set[str], Finding | None]:
    try:
        result = subprocess.run(
            ["git", *args, "-z"],
            cwd=root,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set(), Finding("git_diff_failed", ".", "无法读取Git变化路径")
    if result.returncode != 0:
        return set(), Finding("git_diff_failed", ".", "Git变化路径命令失败")
    try:
        values = result.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError:
        return set(), Finding("git_path_encoding_invalid", ".", "Git变化路径不是UTF-8")
    return {value for value in values if value}, None


def collect_changed_paths(
    root: Path,
    revision: str,
    policy: DocumentationPolicy,
) -> tuple[set[str] | None, list[Finding]]:
    """合并基线、暂存区、工作区和未跟踪文件，形成完整变化集合。"""

    if ZERO_REVISION.fullmatch(revision):
        return None, []
    timeout = policy.limits["git_timeout_seconds"]
    commands = [
        (["diff", "--name-only", f"{revision}...HEAD"], True),
        (["diff", "--name-only", "--cached"], False),
        (["diff", "--name-only"], False),
        (["ls-files", "--others", "--exclude-standard"], False),
    ]
    paths: set[str] = set()
    findings: list[Finding] = []
    for command, is_base_diff in commands:
        current, error = _git_paths(root, command, timeout)
        paths.update(current)
        if error:
            findings.append(
                Finding(
                    "git_base_invalid" if is_base_diff else error.code,
                    error.path,
                    "Git基线不可解析，未完成差异文档门禁" if is_base_diff else error.message,
                )
            )
            if is_base_diff:
                return None, findings
    return paths, findings


def validate_changed_documentation(
    changed_paths: set[str],
    documents: dict[str, Document],
    policy: DocumentationPolicy,
) -> list[Finding]:
    """要求生产源码变化同步现行模块设计，并为重大变化提供变更设计。"""

    findings: list[Finding] = []
    normalized = {PurePosixPath(path).as_posix() for path in changed_paths}
    packages: set[str] = set()
    for path in sorted(normalized):
        parts = PurePosixPath(path).parts
        if len(parts) >= 4 and parts[:2] == ("src", "harnessix"):
            package = parts[2]
            if package != "__pycache__":
                packages.add(package)
    for package in sorted(packages):
        expected = f"docs/modules/{package.replace('_', '-')}.md"
        if expected not in normalized:
            findings.append(
                Finding(
                    "doc_sync_module_required",
                    expected,
                    f"源码包{package}变化时必须同步现行模块设计",
                )
            )

    for source, targets in policy.root_source_documents.items():
        if source in normalized and not any(target in normalized for target in targets):
            findings.append(
                Finding(
                    "doc_sync_root_required",
                    source,
                    f"根级源码变化时必须同步以下资料之一：{', '.join(targets)}",
                )
            )

    major = len(packages) > 1 or any(
        pattern.search(path) for path in normalized for pattern in policy.major_change_patterns
    )
    changed_designs = sorted(
        path for path in normalized if path.startswith("docs/changes/") and path.endswith(".md")
    )
    if major and not changed_designs:
        findings.append(
            Finding(
                "doc_sync_change_design_required",
                "docs/changes",
                "跨包、合同、状态、持久化或安全变化必须提交重大变更设计",
            )
        )
    for path in changed_designs:
        document = documents.get(path)
        if (
            document is None
            or not document.metadata
            or document.metadata.get("doc_type") != "change-design"
        ):
            findings.append(
                Finding(
                    "doc_sync_change_design_invalid",
                    path,
                    "变化设计必须是可解析的change-design文档",
                )
            )
    return findings


def render_mermaid(
    documents: dict[str, Document],
    policy: DocumentationPolicy,
    *,
    changed_paths: set[str] | None,
) -> list[Finding]:
    """用外部Mermaid CLI渲染变化文档中的图，并验证输出确实生成。"""

    executable = shutil.which("mmdc")
    if executable is None:
        return [Finding("mermaid_renderer_missing", ".", "未找到mmdc，无法执行Mermaid渲染")]
    selected = [
        document
        for document in documents.values()
        if changed_paths is None or document.path in changed_paths
    ]
    findings: list[Finding] = []
    render_index = 0
    with tempfile.TemporaryDirectory(prefix="harnessix-doc-mermaid-") as directory:
        temp = Path(directory)
        for document in selected:
            for block in document.mermaid_blocks:
                render_index += 1
                source = temp / f"diagram-{render_index:04d}.mmd"
                output = source.with_suffix(".svg")
                source.write_text(block.source + "\n", encoding="utf-8")
                try:
                    result = subprocess.run(
                        [executable, "-q", "-i", str(source), "-o", str(output)],
                        capture_output=True,
                        timeout=policy.limits["mermaid_timeout_seconds"],
                        check=False,
                        env=os.environ.copy(),
                    )
                except subprocess.TimeoutExpired:
                    findings.append(
                        Finding(
                            "mermaid_render_timeout", document.path, "Mermaid渲染超时", block.line
                        )
                    )
                    continue
                except OSError:
                    findings.append(
                        Finding(
                            "mermaid_render_failed",
                            document.path,
                            "Mermaid渲染器启动失败",
                            block.line,
                        )
                    )
                    continue
                if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                    findings.append(
                        Finding(
                            "mermaid_render_failed",
                            document.path,
                            "Mermaid图未成功生成SVG",
                            block.line,
                        )
                    )
    return findings


def check_repository(
    root: Path,
    policy: DocumentationPolicy,
    *,
    changed_paths: set[str] | None = None,
    verify_git_revisions: bool = True,
    render_diagrams: bool = False,
) -> CheckReport:
    """执行完整静态规则及可选差异、渲染规则。"""

    documents, findings = load_documents(root, policy)
    findings.extend(
        validate_metadata(
            root,
            documents,
            policy,
            verify_git_revisions=verify_git_revisions,
        )
    )
    findings.extend(validate_links(root, documents, policy))
    findings.extend(validate_sections(documents, policy))
    findings.extend(validate_mermaid_structure(documents, policy))
    relation_findings, package_count = validate_repository_relations(root, documents, policy)
    findings.extend(relation_findings)
    if changed_paths is not None:
        findings.extend(validate_changed_documentation(changed_paths, documents, policy))
    if render_diagrams:
        findings.extend(render_mermaid(documents, policy, changed_paths=changed_paths))
    deduplicated = {finding.sort_key(): finding for finding in findings}
    ordered = tuple(sorted(deduplicated.values(), key=Finding.sort_key))
    return CheckReport(
        policy_version=policy.schema_version,
        document_count=len(documents),
        link_count=sum(len(document.links) for document in documents.values()),
        mermaid_count=sum(len(document.mermaid_blocks) for document in documents.values()),
        source_package_count=package_count,
        changed_path_count=len(changed_paths or ()),
        findings=ordered,
    )


def _text_report(report: CheckReport) -> str:
    if report.findings:
        lines = []
        for finding in report.findings:
            location = finding.path + (f":{finding.line}" if finding.line is not None else "")
            lines.append(f"{location} [{finding.code}] {finding.message}")
        lines.append(f"文档门禁失败：{len(report.findings)}项")
        return "\n".join(lines)
    return (
        "文档门禁通过："
        f"{report.document_count}份文档、{report.link_count}条链接、"
        f"{report.mermaid_count}幅Mermaid、{report.source_package_count}个源码包、"
        f"{report.changed_path_count}个变化路径"
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harnessix版本化文档门禁")
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--changed-from")
    parser.add_argument("--render-mermaid", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_console()
    args = _argument_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        policy = load_policy(root, args.policy)
    except PolicyError as exc:
        finding = Finding("policy_invalid", args.policy, str(exc))
        if args.format == "json":
            print(
                json.dumps(
                    {"policy_version": None, "findings": [asdict(finding)]},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"{finding.path} [{finding.code}] {finding.message}")
        return 1

    changed_paths: set[str] | None = None
    git_findings: list[Finding] = []
    if args.changed_from:
        changed_paths, git_findings = collect_changed_paths(root, args.changed_from, policy)
    try:
        report = check_repository(
            root,
            policy,
            changed_paths=changed_paths,
            render_diagrams=args.render_mermaid,
        )
    except Exception as exc:  # pragma: no cover - final safety boundary
        finding = Finding("internal_error", ".", f"文档门禁内部失败：{type(exc).__name__}")
        report = CheckReport(policy.schema_version, 0, 0, 0, 0, 0, (finding,))
    if git_findings:
        merged = {finding.sort_key(): finding for finding in (*report.findings, *git_findings)}
        report = CheckReport(
            report.policy_version,
            report.document_count,
            report.link_count,
            report.mermaid_count,
            report.source_package_count,
            report.changed_path_count,
            tuple(sorted(merged.values(), key=Finding.sort_key)),
        )
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    else:
        print(_text_report(report))
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
