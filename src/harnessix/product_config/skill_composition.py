"""产品Skill装配：发现已存在的本机与工作区Root，把两个只读工具接入统一Action入口。"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.context.contracts import ContextFragment, ContextFragmentKind
from harnessix.product_config.action_composition import FixedProductActionEnvironment
from harnessix.skills import SkillCatalogSnapshot, SkillRegistry, SkillSource, SQLiteSkillStore
from harnessix.skills.actions import build_skill_agent_tools
from harnessix.skills.prompt import render_skill_listing
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.router import TrustedActionRouter

PRODUCT_SKILL_CATALOG_ID = "harnessix.product"
_SKILL_LISTING_SOURCE = "harnessix.skill-catalog/v1"
_SKILL_DIRECTORY = (".harnessix", "skills")

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProductSkills:
    """一次启动冻结的Skill目录与其模型工具入口。"""

    catalog: SkillCatalogSnapshot
    gateway: RouterBackedAgentActionGateway


def product_skill_roots(
    workspace_root: Path, *, home: Path | None = None
) -> tuple[tuple[Literal["user", "workspace"], Path], ...]:
    """产品读取Skill的两个位置；都在受保护的`.harnessix`下，Agent自身不能写入。"""

    return (
        ("user", (home or Path.home()).joinpath(*_SKILL_DIRECTORY)),
        ("workspace", workspace_root.joinpath(*_SKILL_DIRECTORY)),
    )


def product_skill_sources(
    workspace_root: Path, *, home: Path | None = None
) -> tuple[SkillSource, ...]:
    """只收录已存在的Root，不创建目录；没有Root时产品行为与未装配Skill相同。"""

    sources: list[SkillSource] = []
    for kind, root in product_skill_roots(workspace_root, home=home):
        if not os.path.lexists(root):
            continue
        try:
            sources.append(SkillSource(kind, kind, root))
        except KernelError as error:
            _logger.warning("skill root ignored: kind=%s code=%s", kind, error.code)
    return tuple(sources)


@contextmanager
def open_product_skills(
    router: TrustedActionRouter,
    environment: FixedProductActionEnvironment,
    sources: tuple[SkillSource, ...],
) -> Iterator[ProductSkills | None]:
    """目录快照只活在本进程：备份只接受固定清单，调用审计已由Action Audit持久化。"""

    if not sources:
        yield None
        return
    with (
        TemporaryDirectory(prefix="harnessix-skills-") as directory,
        SQLiteSkillStore(Path(directory) / "skills.db") as store,
    ):
        registry = SkillRegistry(catalog_id=PRODUCT_SKILL_CATALOG_ID, sources=sources, store=store)
        try:
            catalog = registry.discover()
        except KernelError as error:
            # 坏的Skill目录不应让Agent无法启动；`harnessix skills list`可看到同一错误。
            _logger.warning("skill discovery failed: code=%s", error.code)
            yield None
            return
        if not catalog.skills:
            yield None
            return
        tools = build_skill_agent_tools(registry, catalog)
        router.register_many(tools.definitions)
        gateway = RouterBackedAgentActionGateway(
            router,
            tools.descriptors,
            lambda thread, _turn, _call: environment.context(thread.workspace),
            source="skill",
            source_id=PRODUCT_SKILL_CATALOG_ID,
        )
        try:
            yield ProductSkills(catalog, gateway)
        finally:
            gateway.close()


def skill_context_fragments(
    skills: ProductSkills | None, *, tools_visible: bool = True
) -> tuple[ContextFragment, ...]:
    """Skill元数据不受信，按项目指令优先级进入Context；工作流视图隐藏工具时不宣告。"""

    listing = None if skills is None or not tools_visible else render_skill_listing(skills.catalog)
    if listing is None:
        return ()
    return (
        ContextFragment(
            kind=ContextFragmentKind.PROJECT_INSTRUCTION,
            source=_SKILL_LISTING_SOURCE,
            content=listing,
        ),
    )
