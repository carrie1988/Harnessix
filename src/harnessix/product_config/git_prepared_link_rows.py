"""原 GitDB 全前缀认证后的 prepared 业务投影；拒绝未知状态及未实现业务表。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_rows import GitPrefixRows, capture_git_prefix_rows
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_proof import prepared_link_changed
from harnessix.product_config.git_prepared_link_wire import decode_product_git_prepared_link
from harnessix.session.store_publication import SessionPublicationBinding

_UNIMPLEMENTED_TABLES = (
    "git_native_bridge_index",
    "git_object_inventories",
    "git_object_inventory_events",
    "git_worktrees",
    "git_worktree_events",
    "git_checkpoints",
    "git_commits",
    "git_commit_events",
)


def prepared_link_columns(link: ProductGitPreparedLink, body: bytes) -> tuple[str | int, ...]:
    """原十一列完整投影；正文与所有冗余归属字段必须同时一致。"""
    core, route = link.plan.core, link.plan.route
    return (
        str(route.execution.plan_id),
        str(core.delivery_id),
        str(core.thread_id),
        str(core.turn_id),
        str(core.call.call_id),
        "checkpoint" if core.commit_spec is None else "commit",
        core.fingerprint,
        route.fingerprint,
        link.phase,
        link.sequence,
        body.decode("utf-8", "strict"),
    )


def read_prepared_link_rows(
    database: sqlite3.Connection,
    publication: SessionPublicationBinding,
    *,
    checkpoint: Callable[[], None],
) -> tuple[tuple[ProductGitPreparedLink, ...], GitPrefixRows]:
    """先验证全表 MAC/尾锚，再解析全部正文；本入口不是全 Git 生命周期 Reader。"""
    catalog = read_git_prefix_catalog(
        database, publication.git_verifier, publication.git_prefix_verifier, checkpoint=checkpoint
    )
    rows = capture_git_prefix_rows(database, checkpoint=checkpoint)
    if rows.tables != catalog.tables:
        raise prepared_link_changed()
    if any(rows.table(table) for table in _UNIMPLEMENTED_TABLES):
        raise KernelError("git_prepared_link_scope_unsupported", "Git账本超出待审批回读范围")
    streams = {str(stream.first.record_id): stream for stream in catalog.streams}
    result = []
    for row in rows.table("git_product_links"):
        checkpoint()
        if type(row[-1]) is not str:
            raise prepared_link_changed()
        link = decode_product_git_prepared_link(row[-1].encode("utf-8"), checkpoint=checkpoint)
        core, route = link.plan.core, link.plan.route
        if row != prepared_link_columns(link, row[-1].encode("utf-8")):
            raise prepared_link_changed()
        stream = streams.get(str(route.execution.plan_id))
        if (
            stream is None
            or stream.count != 1
            or (
                stream.first.record_kind,
                stream.first.record_id,
                stream.first.route_id,
                stream.first.delivery_id,
                stream.first.thread_id,
                stream.first.turn_id,
                stream.first.call_id,
            )
            != (
                "product_link",
                route.execution.plan_id,
                route.execution.plan_id,
                core.delivery_id,
                core.thread_id,
                core.turn_id,
                core.call.call_id,
            )
        ):
            raise prepared_link_changed()
        result.append(link)
    if len(streams) != len(result):
        raise prepared_link_changed()
    checkpoint()
    return tuple(result), rows
