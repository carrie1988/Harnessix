from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.skills.contracts import (
    SkillCatalogSnapshot,
    SkillContent,
    SkillDiscoveryIssue,
    SkillLoadInput,
    SkillManifestSnapshot,
    SkillNameConflict,
    SkillResourceContent,
    SkillResourceReadInput,
    SkillSourceKind,
    SkillSourceSnapshot,
    skill_catalog_snapshot_digest,
    skill_manifest_snapshot_digest,
    skill_source_snapshot_digest,
)
from harnessix.skills.store import SQLiteSkillStore
from harnessix.workspace.snapshot import SecureWorkspaceReader

MAX_SKILL_DOCUMENT_BYTES = 256 * 1024
MAX_SKILL_RESOURCE_BYTES = 64 * 1024
MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_FRONTMATTER_LINES = 256
MAX_DISCOVERY_DEPTH = 6
MAX_DISCOVERY_DIRECTORIES = 2048
MAX_DISCOVERY_ENTRIES = 8192
MAX_SKILLS = 2048
MAX_RESOURCES = 64
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".git",
        ".ssh",
        ".aws",
        ".gnupg",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class SkillSource:
    source_id: str
    kind: SkillSourceKind
    root: Path

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.source_id) is None or self.kind not in {
            "bundled",
            "user",
            "workspace",
        }:
            raise KernelError("skill_source_invalid", "Skill来源身份无效")
        if not self.root.is_absolute():
            raise KernelError("skill_source_invalid", "Skill来源Root必须是绝对路径")
        try:
            info = self.root.stat(follow_symlinks=False)
            is_junction = bool(getattr(self.root, "is_junction", lambda: False)())
        except OSError:
            raise KernelError("skill_source_unavailable", "Skill来源Root不可用") from None
        if not stat.S_ISDIR(info.st_mode) or self.root.is_symlink() or is_junction:
            raise KernelError("skill_source_invalid", "Skill来源Root必须是普通目录")


@dataclass(frozen=True, slots=True)
class _ParsedSkill:
    name: str
    description: str
    version: str | None
    body: str


class SkillRegistry:
    """显式Root、不可变目录与按摘要复核的Skill读取运行时。"""

    def __init__(
        self,
        *,
        catalog_id: str,
        sources: tuple[SkillSource, ...],
        store: SQLiteSkillStore,
    ) -> None:
        if not sources or len(sources) > 32:
            raise KernelError("skill_source_invalid", "Skill来源数量必须为1到32")
        if len({source.source_id for source in sources}) != len(sources):
            raise KernelError("skill_source_duplicate", "Skill来源身份重复")
        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", catalog_id) is None:
            raise KernelError("skill_catalog_invalid", "Skill目录身份无效")
        self._catalog_id = catalog_id
        self._sources = {source.source_id: source for source in sources}
        self._store = store
        self._lock = threading.RLock()

    def discover(self) -> SkillCatalogSnapshot:
        with self._lock:
            manifests: list[SkillManifestSnapshot] = []
            source_snapshots: list[SkillSourceSnapshot] = []
            issues: list[SkillDiscoveryIssue] = []
            for source in sorted(self._sources.values(), key=lambda item: item.source_id):
                source_manifests, source_issues, root_sha256, source_revision = (
                    self._discover_source(source)
                )
                manifests.extend(source_manifests)
                issues.extend(source_issues)
                source_candidate = SkillSourceSnapshot.model_construct(
                    _fields_set=None,
                    source_id=source.source_id,
                    kind=source.kind,
                    root_sha256=root_sha256,
                    source_revision=source_revision,
                    skill_count=len(source_manifests),
                    source_sha256="0" * 64,
                )
                source_snapshots.append(
                    SkillSourceSnapshot(
                        **source_candidate.model_dump(exclude={"source_sha256"}),
                        source_sha256=skill_source_snapshot_digest(source_candidate),
                    )
                )
            if len(manifests) > MAX_SKILLS:
                raise KernelError("skill_catalog_limit", "Skill目录数量超过上限")
            manifests.sort(key=lambda item: item.qualified_name)
            conflicts = tuple(
                SkillNameConflict(name=name, qualified_names=tuple(sorted(items)))
                for name, items in sorted(self._name_index(manifests).items())
                if len(items) > 1
            )
            generation = self._store.next_generation(self._catalog_id)
            catalog_candidate = SkillCatalogSnapshot.model_construct(
                _fields_set=None,
                catalog_id=self._catalog_id,
                generation=generation,
                captured_at=utc_now(),
                sources=tuple(source_snapshots),
                skills=tuple(manifests),
                conflicts=conflicts,
                issues=tuple(
                    sorted(issues, key=lambda item: (item.source_id, item.path_sha256, item.code))
                ),
                catalog_sha256="0" * 64,
            )
            catalog = SkillCatalogSnapshot(
                **catalog_candidate.model_dump(exclude={"catalog_sha256"}),
                catalog_sha256=skill_catalog_snapshot_digest(catalog_candidate),
            )
            return self._store.save_catalog(catalog)

    def catalog(self, *, digest: str | None = None) -> SkillCatalogSnapshot:
        return self._store.load_catalog(self._catalog_id, digest=digest)

    def resolve(
        self,
        catalog: SkillCatalogSnapshot,
        name: str,
        expected_manifest_sha256: str,
    ) -> SkillManifestSnapshot:
        if catalog.catalog_id != self._catalog_id:
            raise KernelError("skill_catalog_mismatch", "Skill目录不属于当前运行时")
        if "/" in name:
            matches = [item for item in catalog.skills if item.qualified_name == name]
        else:
            matches = [item for item in catalog.skills if item.name == name]
            if len(matches) > 1:
                raise KernelError("skill_name_conflict", "Skill普通名称存在冲突，必须使用限定名称")
        if not matches:
            raise KernelError("skill_not_found", "Skill不存在")
        selected = matches[0]
        if selected.manifest_sha256 != expected_manifest_sha256:
            raise KernelError("skill_contract_changed", "Skill调用绑定的清单摘要不一致")
        return selected

    def load(self, request: SkillLoadInput) -> SkillContent:
        checked = SkillLoadInput.model_validate_json(request.model_dump_json())
        catalog = self.catalog(digest=checked.catalog_sha256)
        manifest = self.resolve(catalog, checked.name, checked.expected_manifest_sha256)
        try:
            source, reader = self._bound_reader(catalog, manifest)
            with reader:
                current, parsed = self._read_manifest(source, reader, manifest.relative_path)
                if current != manifest:
                    raise KernelError("skill_content_changed", "Skill正文在目录捕获后发生变化")
                resources = self._resources(reader, manifest.relative_path)
            result = SkillContent(
                catalog_sha256=catalog.catalog_sha256,
                manifest_sha256=manifest.manifest_sha256,
                qualified_name=manifest.qualified_name,
                effective_version=manifest.effective_version,
                content=parsed.body,
                content_sha256=hashlib.sha256(parsed.body.encode()).hexdigest(),
                resources=resources,
            )
        except KernelError as error:
            self._record_failure(catalog, manifest, "load", None, error.code)
            raise
        self._store.record_access(
            catalog=catalog,
            operation="load",
            manifest_sha256=manifest.manifest_sha256,
            resource_path=None,
            outcome="succeeded",
            result_sha256=canonical_digest(result.model_dump(mode="json", exclude={"content"})),
        )
        return result

    def read_resource(self, request: SkillResourceReadInput) -> SkillResourceContent:
        checked = SkillResourceReadInput.model_validate_json(request.model_dump_json())
        self._validate_resource_path(checked.path)
        catalog = self.catalog(digest=checked.catalog_sha256)
        manifest = self.resolve(catalog, checked.name, checked.expected_manifest_sha256)
        try:
            source, reader = self._bound_reader(catalog, manifest)
            with reader:
                current, _ = self._read_manifest(source, reader, manifest.relative_path)
                if current != manifest:
                    raise KernelError("skill_content_changed", "Skill正文在目录捕获后发生变化")
                resources = self._resources(reader, manifest.relative_path)
                if checked.path not in resources:
                    raise KernelError("skill_resource_not_found", "Skill资源不存在或不可读取")
                manifest_dir = manifest.relative_path.rpartition("/")[0]
                full_path = f"{manifest_dir}/{checked.path}" if manifest_dir else checked.path
                body = self._read_utf8(reader, full_path, MAX_SKILL_RESOURCE_BYTES)
            result = SkillResourceContent(
                catalog_sha256=catalog.catalog_sha256,
                manifest_sha256=manifest.manifest_sha256,
                qualified_name=manifest.qualified_name,
                path=checked.path,
                content=body,
                content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            )
        except KernelError as error:
            self._record_failure(catalog, manifest, "read_resource", checked.path, error.code)
            raise
        self._store.record_access(
            catalog=catalog,
            operation="read_resource",
            manifest_sha256=manifest.manifest_sha256,
            resource_path=checked.path,
            outcome="succeeded",
            result_sha256=canonical_digest(result.model_dump(mode="json", exclude={"content"})),
        )
        return result

    def _discover_source(
        self, source: SkillSource
    ) -> tuple[list[SkillManifestSnapshot], list[SkillDiscoveryIssue], str, str]:
        issues: list[SkillDiscoveryIssue] = []
        manifests: list[SkillManifestSnapshot] = []
        try:
            with SecureWorkspaceReader(source.root) as reader:
                root_sha256 = canonical_digest(
                    {"path": self._path_key(reader.path), "identity": reader.root_identity}
                )
                paths, scan_issues = self._manifest_paths(source, reader)
                issues.extend(scan_issues)
                for path in paths:
                    try:
                        manifest, _ = self._read_manifest(source, reader, path)
                        manifests.append(manifest)
                    except KernelError as error:
                        issues.append(self._issue(source.source_id, path, error.code))
        except KernelError as error:
            raise self._reader_error(error, limit_code="skill_catalog_limit") from None
        groups: dict[str, list[SkillManifestSnapshot]] = defaultdict(list)
        for manifest in manifests:
            groups[manifest.qualified_name].append(manifest)
        valid: list[SkillManifestSnapshot] = []
        for items in groups.values():
            if len(items) == 1:
                valid.append(items[0])
                continue
            issues.extend(
                self._issue(source.source_id, item.relative_path, "skill_duplicate_qualified_name")
                for item in items
            )
        valid.sort(key=lambda item: item.qualified_name)
        source_revision = canonical_digest(
            {
                "root": root_sha256,
                "skills": [item.manifest_sha256 for item in valid],
                "issues": [
                    (item.path_sha256, item.code)
                    for item in sorted(issues, key=lambda item: (item.path_sha256, item.code))
                ],
            }
        )
        return valid, issues, root_sha256, source_revision

    def _manifest_paths(
        self, source: SkillSource, reader: SecureWorkspaceReader
    ) -> tuple[list[str], list[SkillDiscoveryIssue]]:
        queue: deque[tuple[str, int]] = deque([(".", 0)])
        paths: list[str] = []
        issues: list[SkillDiscoveryIssue] = []
        directories = 0
        entries_seen = 0
        while queue:
            directory, depth = queue.popleft()
            directories += 1
            if directories > MAX_DISCOVERY_DIRECTORIES:
                raise KernelError("skill_catalog_limit", "Skill发现目录数量超过上限")
            try:
                entries = reader.list_directory(directory, max_entries=MAX_DISCOVERY_ENTRIES)
            except KernelError as error:
                raise self._reader_error(error, limit_code="skill_catalog_limit") from None
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_DISCOVERY_ENTRIES:
                    raise KernelError("skill_catalog_limit", "Skill发现条目数量超过上限")
                path = entry.name if directory == "." else f"{directory}/{entry.name}"
                if self._sensitive_path(path):
                    issues.append(
                        self._issue(source.source_id, path, "skill_sensitive_path_denied")
                    )
                    continue
                if entry.kind in {"symlink", "special"}:
                    issues.append(self._issue(source.source_id, path, "skill_path_denied"))
                    continue
                if entry.kind == "directory":
                    if depth >= MAX_DISCOVERY_DEPTH:
                        issues.append(self._issue(source.source_id, path, "skill_discovery_depth"))
                    else:
                        queue.append((path, depth + 1))
                    continue
                filename_matches = (
                    entry.name == "SKILL.md"
                    if reader.platform == "posix"
                    else entry.name.casefold() == "skill.md"
                )
                if filename_matches:
                    paths.append(path)
                    if len(paths) > MAX_SKILLS:
                        raise KernelError("skill_catalog_limit", "Skill发现数量超过上限")
        return sorted(
            paths, key=lambda item: item if reader.platform == "posix" else item.casefold()
        ), issues

    def _bound_reader(
        self, catalog: SkillCatalogSnapshot, manifest: SkillManifestSnapshot
    ) -> tuple[SkillSource, SecureWorkspaceReader]:
        source = self._sources.get(manifest.source_id)
        snapshot = next(
            (item for item in catalog.sources if item.source_id == manifest.source_id), None
        )
        if source is None or snapshot is None or source.kind != manifest.source_kind:
            raise KernelError("skill_source_changed", "Skill来源绑定已经变化")
        try:
            reader = SecureWorkspaceReader(source.root)
        except KernelError as error:
            raise self._reader_error(error, limit_code="skill_source_unavailable") from None
        root_sha256 = canonical_digest(
            {"path": self._path_key(reader.path), "identity": reader.root_identity}
        )
        if root_sha256 != snapshot.root_sha256:
            reader.close()
            raise KernelError("skill_source_changed", "Skill来源Root已经变化")
        return source, reader

    def _read_manifest(
        self,
        source: SkillSource,
        reader: SecureWorkspaceReader,
        relative_path: str,
    ) -> tuple[SkillManifestSnapshot, _ParsedSkill]:
        try:
            raw = reader.read_file(relative_path, max_bytes=MAX_SKILL_DOCUMENT_BYTES)
        except KernelError as error:
            raise self._reader_error(error, limit_code="skill_content_limit") from None
        parsed = self._parse_skill(raw, relative_path)
        metadata_sha256 = canonical_digest(
            {"name": parsed.name, "description": parsed.description, "version": parsed.version}
        )
        content_sha256 = hashlib.sha256(raw).hexdigest()
        candidate = SkillManifestSnapshot.model_construct(
            _fields_set=None,
            source_id=source.source_id,
            source_kind=source.kind,
            name=parsed.name,
            qualified_name=f"{source.source_id}/{parsed.name}",
            description=parsed.description,
            declared_version=parsed.version,
            effective_version=parsed.version or f"sha256:{content_sha256[:16]}",
            relative_path=relative_path,
            content_utf8_bytes=len(raw),
            content_sha256=content_sha256,
            metadata_sha256=metadata_sha256,
            manifest_sha256="0" * 64,
        )
        try:
            manifest = SkillManifestSnapshot(
                **candidate.model_dump(exclude={"manifest_sha256"}),
                manifest_sha256=skill_manifest_snapshot_digest(candidate),
            )
        except ValidationError:
            raise KernelError("skill_manifest_invalid", "Skill清单元数据无效") from None
        return manifest, parsed

    @staticmethod
    def _parse_skill(raw: bytes, relative_path: str) -> _ParsedSkill:
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise KernelError("skill_invalid_utf8", "Skill文档必须是有效UTF-8") from None
        if "\x00" in text:
            raise KernelError("skill_manifest_invalid", "Skill文档不能包含NUL")
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise KernelError("skill_frontmatter_missing", "Skill文档缺少YAML Frontmatter")
        closing: int | None = None
        frontmatter_bytes = 0
        for index, line in enumerate(lines[1 : MAX_FRONTMATTER_LINES + 1], start=1):
            if line.strip() == "---":
                closing = index
                break
            frontmatter_bytes += len(line.encode())
            if frontmatter_bytes > MAX_FRONTMATTER_BYTES:
                raise KernelError("skill_frontmatter_limit", "Skill Frontmatter超过上限")
        if closing is None:
            raise KernelError("skill_frontmatter_invalid", "Skill Frontmatter缺少结束分隔符")
        try:
            loaded: Any = yaml.load("".join(lines[1:closing]), Loader=_UniqueKeySafeLoader)
        except yaml.YAMLError:
            raise KernelError(
                "skill_frontmatter_invalid", "Skill Frontmatter不是安全YAML"
            ) from None
        if not isinstance(loaded, dict) or len(loaded) > 64:
            raise KernelError("skill_frontmatter_invalid", "Skill Frontmatter必须是有界对象")
        SkillRegistry._validate_yaml_shape(loaded)
        fallback = relative_path.rpartition("/")[0].rpartition("/")[2]
        raw_name = loaded.get("name", fallback)
        raw_description = loaded.get("description")
        raw_version = loaded.get("version")
        if not isinstance(raw_name, str) or not isinstance(raw_description, str):
            raise KernelError("skill_manifest_invalid", "Skill名称和描述必须是文本")
        if raw_version is not None and not isinstance(raw_version, str | int):
            raise KernelError("skill_manifest_invalid", "Skill版本必须是文本或整数")
        name = " ".join(raw_name.split())
        description = " ".join(raw_description.split())
        version = None if raw_version is None else " ".join(str(raw_version).split())
        body = "".join(lines[closing + 1 :]).strip()
        if not body:
            raise KernelError("skill_content_empty", "Skill正文不能为空")
        if len(body.encode()) > MAX_SKILL_DOCUMENT_BYTES:
            raise KernelError("skill_content_limit", "Skill正文超过上限")
        return _ParsedSkill(name=name, description=description, version=version, body=body)

    @staticmethod
    def _validate_yaml_shape(
        value: object, *, depth: int = 0, nodes: list[int] | None = None
    ) -> None:
        counter = nodes if nodes is not None else [0]
        counter[0] += 1
        if counter[0] > 512 or depth > 16:
            raise KernelError("skill_frontmatter_limit", "Skill Frontmatter结构超过上限")
        if isinstance(value, dict):
            if any(not isinstance(key, str) or len(key) > 128 for key in value):
                raise KernelError("skill_frontmatter_invalid", "Skill Frontmatter键无效")
            for child in value.values():
                SkillRegistry._validate_yaml_shape(child, depth=depth + 1, nodes=counter)
        elif isinstance(value, list):
            if len(value) > 256:
                raise KernelError("skill_frontmatter_limit", "Skill Frontmatter数组超过上限")
            for child in value:
                SkillRegistry._validate_yaml_shape(child, depth=depth + 1, nodes=counter)
        elif not isinstance(value, str | int | float | bool | None):
            raise KernelError("skill_frontmatter_invalid", "Skill Frontmatter值类型无效")

    def _resources(self, reader: SecureWorkspaceReader, manifest_path: str) -> tuple[str, ...]:
        manifest_dir = manifest_path.rpartition("/")[0] or "."
        queue: deque[tuple[str, str, int]] = deque([(manifest_dir, "", 0)])
        resources: list[str] = []
        while queue:
            directory, relative, depth = queue.popleft()
            try:
                entries = reader.list_directory(directory, max_entries=MAX_DISCOVERY_ENTRIES)
            except KernelError as error:
                raise self._reader_error(error, limit_code="skill_resource_limit") from None
            if relative and any(
                entry.name.casefold() == "skill.md" and entry.kind == "file" for entry in entries
            ):
                continue
            for entry in entries:
                child_relative = entry.name if not relative else f"{relative}/{entry.name}"
                full_path = entry.name if directory == "." else f"{directory}/{entry.name}"
                if entry.name.casefold() == "skill.md" or self._sensitive_path(child_relative):
                    continue
                if entry.kind == "directory" and depth < MAX_DISCOVERY_DEPTH:
                    queue.append((full_path, child_relative, depth + 1))
                elif entry.kind == "file":
                    self._validate_resource_path(child_relative)
                    resources.append(child_relative)
                    if len(resources) > MAX_RESOURCES:
                        raise KernelError("skill_resource_limit", "Skill资源数量超过上限")
        return tuple(sorted(resources))

    @staticmethod
    def _read_utf8(reader: SecureWorkspaceReader, path: str, maximum: int) -> str:
        try:
            raw = reader.read_file(path, max_bytes=maximum)
        except KernelError as error:
            raise SkillRegistry._reader_error(error, limit_code="skill_resource_limit") from None
        try:
            body = raw.decode("utf-8")
        except UnicodeError:
            raise KernelError("skill_resource_invalid_utf8", "Skill资源必须是UTF-8文本") from None
        if "\x00" in body:
            raise KernelError("skill_resource_invalid_utf8", "Skill资源不能包含NUL")
        return body

    @classmethod
    def _validate_resource_path(cls, path: str) -> None:
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
            or cls._sensitive_path(path)
        ):
            raise KernelError("skill_resource_path_denied", "Skill资源路径不受允许")

    @staticmethod
    def _sensitive_path(path: str) -> bool:
        return any(
            part.casefold() in _SENSITIVE_NAMES
            or part.casefold().startswith(".env.")
            or part.casefold().endswith(_SENSITIVE_SUFFIXES)
            for part in path.split("/")
        )

    @staticmethod
    def _path_key(path: Path) -> str:
        value = os.path.abspath(path)
        return value if os.name == "posix" else value.casefold()

    @staticmethod
    def _name_index(skills: list[SkillManifestSnapshot]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = defaultdict(list)
        for skill in skills:
            result[skill.name].append(skill.qualified_name)
        return result

    @staticmethod
    def _issue(source_id: str, path: str, code: str) -> SkillDiscoveryIssue:
        normalized = code if code.startswith("skill_") else f"skill_{code}"
        return SkillDiscoveryIssue(
            source_id=source_id,
            code=normalized,
            path_sha256=canonical_digest(path),
        )

    def _record_failure(
        self,
        catalog: SkillCatalogSnapshot,
        manifest: SkillManifestSnapshot,
        operation: str,
        resource_path: str | None,
        error_code: str,
    ) -> None:
        safe_code = error_code if error_code.startswith("skill_") else "skill_access_failed"
        self._store.record_access(
            catalog=catalog,
            operation=operation,  # type: ignore[arg-type]
            manifest_sha256=manifest.manifest_sha256,
            resource_path=resource_path,
            outcome="failed",
            error_code=safe_code,
        )

    @staticmethod
    def _reader_error(error: KernelError, *, limit_code: str) -> KernelError:
        if error.code in {"workspace_changed", "workspace_observation_failed"}:
            return KernelError("skill_source_changed", "Skill来源在读取期间发生变化")
        if error.code == "workspace_snapshot_limit":
            return KernelError(limit_code, "Skill读取超过上限")
        if error.code in {"workspace_path_denied", "workspace_wrong_file_type"}:
            return KernelError("skill_path_denied", "Skill路径不受允许")
        if error.code in {"workspace_parent_missing", "workspace_binding_invalid"}:
            return KernelError("skill_source_unavailable", "Skill来源不可用")
        return KernelError("skill_read_failed", "Skill安全读取失败")
