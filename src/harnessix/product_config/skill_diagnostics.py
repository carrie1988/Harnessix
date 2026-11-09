"""生成Skill诊断清单：Agent启动时可用的Skill，以及未被采用的文件和原因。"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.skill_composition import (
    PRODUCT_SKILL_CATALOG_ID,
    product_skill_roots,
    product_skill_sources,
)
from harnessix.skills import SkillCatalogSnapshot, SkillRegistry, SQLiteSkillStore


def _issue_path(root: Path, path_sha256: str) -> str:
    """Issue只保存路径摘要；展示时在用户自己的Root里反查相对路径。"""
    for directory, _, files in os.walk(root):
        for name in files:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if canonical_digest(relative) == path_sha256:
                return relative
    return "未知文件"


def _render(catalog: SkillCatalogSnapshot, roots: dict[str, Path]) -> list[str]:
    conflicted = {item.name for item in catalog.conflicts}
    lines = []
    for skill in catalog.skills:
        name = skill.qualified_name if skill.name in conflicted else skill.name
        lines.append(f"{name}  [{skill.source_kind}]  {' '.join(skill.description.split())}")
    if not lines:
        lines.append("没有可用的 Skill。")
    for conflict in catalog.conflicts:
        lines.append(f"名称冲突：{conflict.name}，请使用 {'、'.join(conflict.qualified_names)}")
    for issue in catalog.issues:
        path = _issue_path(roots[issue.source_id], issue.path_sha256)
        lines.append(f"未采用：{issue.source_id}/{path}（{issue.code}）")
    return lines


def list_product_skills(workspace: Path, *, home: Path | None = None) -> list[str]:
    """用与Agent启动相同的来源和解析规则生成清单；快照写入临时目录，不碰状态库。"""
    resolved = workspace.resolve()
    guidance = [f"Skill 目录：{root}" for _, root in product_skill_roots(resolved, home=home)]
    sources = product_skill_sources(resolved, home=home)
    if not sources:
        return [*guidance, "没有可用的 Skill。"]
    with (
        TemporaryDirectory(prefix="harnessix-skills-") as directory,
        SQLiteSkillStore(Path(directory) / "skills.db") as store,
    ):
        try:
            catalog = SkillRegistry(
                catalog_id=PRODUCT_SKILL_CATALOG_ID, sources=sources, store=store
            ).discover()
        except KernelError as error:
            return [*guidance, f"Skill 发现失败：{error.code}"]
    return [*guidance, *_render(catalog, {item.source_id: item.root for item in sources})]
