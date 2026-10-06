"""GitDB 全集规范目录；只描述认证覆盖，不授予产品归属、批准或执行权。"""

from __future__ import annotations

import json
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import DeliveryContract
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from harnessix.tools.contracts import Revision

GIT_PREFIX_TABLES = (
    "git_commit_events",
    "git_commits",
    "git_delivery_metadata",
    "git_native_bridge_index",
    "git_object_inventories",
    "git_object_inventory_events",
    "git_product_link_events",
    "git_product_links",
    "git_record_publications",
    "git_checkpoints",
    "git_worktree_events",
    "git_worktrees",
)
# 目录与认证端口沿用完整历史字节上限，不提高领域单记录或捕获额度。
MAX_GIT_PREFIX_BYTES = 64 * 1024 * 1024


class GitPrefixTable(DeliveryContract):
    """覆盖一张表所有列及全部行；包括冗余索引，不只认证payload。"""

    table: str
    rows: int = Field(ge=0, le=100_000)
    sha256: Revision


class GitPrefixStream(DeliveryContract):
    """单实体的唯一epoch及完整从零开始领域事件；第一条认证序号为一。"""

    first: GitDeliveryRecordClaims
    count: int = Field(ge=1, le=100_000)
    last_body_sha256: Revision
    prefix_sha256: Revision

    @model_validator(mode="after")
    def initial(self) -> Self:
        if self.first.sequence != 1 or self.first.previous_sha256 != "0" * 64:
            raise ValueError("Git目录首事件位置无效")
        return self


class GitPrefixCatalog(DeliveryContract):
    """独立尾锚签发的完整目录；它不是业务备份快照或物理执行绑定。"""

    spec_version: Literal["harnessix.git-prefix-catalog/v1"] = "harnessix.git-prefix-catalog/v1"
    schema_version: Literal["2"] = "2"
    store_id: UUID
    key_id: UUID
    genesis_epoch: UUID
    revision: int = Field(ge=0, le=2**63 - 1)
    tables: tuple[GitPrefixTable, ...] = Field(min_length=12, max_length=12)
    streams: tuple[GitPrefixStream, ...] = Field(max_length=100_000)

    @model_validator(mode="after")
    def complete(self) -> Self:
        if tuple(t.table for t in self.tables) != GIT_PREFIX_TABLES:
            raise ValueError("Git目录没有覆盖完整固定表集合")
        keys = [(s.first.record_kind, s.first.record_id.hex) for s in self.streams]
        if keys != sorted(set(keys)) or sum(s.count for s in self.streams) != self.revision:
            raise ValueError("Git目录实体、epoch或事件总数无效")
        return self


def encode_git_prefix_catalog(value: GitPrefixCatalog) -> bytes:
    """产生唯一完整UTF-8编码；私有正文不得截断或作为公开输出。"""
    body = json.dumps(
        value.model_dump(mode="json", warnings="error"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(body) > MAX_GIT_PREFIX_BYTES:
        raise KernelError("git_prefix_catalog_limit", "Git认证目录超出完整历史上限")
    return body


def decode_git_prefix_catalog(body: bytes) -> GitPrefixCatalog:
    """在MAC验真之后解析，规范回编码拒绝重复键、额外字段及别名。"""
    try:
        if type(body) is not bytes or not 1 <= len(body) <= MAX_GIT_PREFIX_BYTES:
            raise ValueError
        catalog = GitPrefixCatalog.model_validate_json(body, strict=True)
        if encode_git_prefix_catalog(catalog) != body:
            raise ValueError
        return catalog
    except (ValueError, TypeError, UnicodeError):
        raise KernelError("publication_history_unproven", "Git认证目录无法核验") from None
