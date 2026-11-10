"""产品扩展装配：Skill（ADR 0125）与本机MCP（ADR 0126）一起接入统一Action入口。

组合根只持有这一个扩展资源；没有Skill目录、也没有已信任的MCP Server时产品行为不变。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.ports import TrustedActionGateway
from harnessix.product_config.action_composition import FixedProductActionEnvironment
from harnessix.product_config.mcp_composition import ProductMcp, open_product_mcp
from harnessix.product_config.mcp_config import ResolvedMcpServer
from harnessix.product_config.mcp_status import trusted_mcp_servers
from harnessix.product_config.skill_composition import (
    ProductSkills,
    open_product_skills,
    product_skill_sources,
)
from harnessix.skills import SkillSource
from harnessix.trusted_actions.router import TrustedActionRouter


@dataclass(frozen=True, slots=True)
class ProductExtensionSources:
    """启动前确定的扩展来源：已存在的Skill目录、已信任且可启动的MCP Server。"""

    skills: tuple[SkillSource, ...] = ()
    mcp_servers: tuple[ResolvedMcpServer, ...] = ()


NO_EXTENSIONS = ProductExtensionSources()


@dataclass(frozen=True, slots=True)
class ProductExtensions:
    skills: ProductSkills | None = None
    mcp: ProductMcp | None = None

    def gateways(self) -> tuple[TrustedActionGateway, ...]:
        skill = () if self.skills is None else (self.skills.gateway,)
        return (*skill, *(() if self.mcp is None else self.mcp.gateways))


def product_extension_sources(
    workspace_root: Path, environ: Mapping[str, str] = os.environ
) -> ProductExtensionSources:
    return ProductExtensionSources(
        skills=product_skill_sources(workspace_root),
        mcp_servers=trusted_mcp_servers(environ),
    )


@asynccontextmanager
async def open_product_extensions(
    router: TrustedActionRouter,
    environment: FixedProductActionEnvironment,
    workspace_root: Path,
    sources: ProductExtensionSources,
) -> AsyncIterator[ProductExtensions]:
    with open_product_skills(router, environment, sources.skills) as skills:
        async with open_product_mcp(
            router, environment, workspace_root, sources.mcp_servers
        ) as mcp:
            yield ProductExtensions(skills, mcp)
