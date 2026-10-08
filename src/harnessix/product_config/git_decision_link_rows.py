"""原 GitDB 中 prepared 和唯一后继决定的完整回读；物理认证不代替业务来源核验。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_decision_link_contracts import ProductGitDecisionLink
from harnessix.product_config.git_decision_link_wire import decode_product_git_decision_link
from harnessix.product_config.git_prefix_catalog import GitPrefixStream
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_rows import GitPrefixRows, Row, capture_git_prefix_rows
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_rows import (
    _UNIMPLEMENTED_TABLES,
    prepared_link_columns,
)
from harnessix.product_config.git_prepared_link_wire import decode_product_git_prepared_link
from harnessix.session.store_publication import SessionPublicationBinding


@dataclass(frozen=True, slots=True)
class GitLinkHistory:
    """同一原关联的完整前驱与可选决定；不持有签发标记、Owner 或执行能力。"""

    prepared: ProductGitPreparedLink = field(repr=False)
    decision: ProductGitDecisionLink | None = field(default=None, repr=False)


def link_history_changed() -> KernelError:
    """不在错误中发布私有正文、事件索引或底层 SQL。"""
    return KernelError("git_decision_link_history_changed", "Git决定关联历史无法核验")


def _read_stream(
    row: Row,
    events: tuple[Row, ...],
    stream: GitPrefixStream,
    check: Callable[[], None],
) -> GitLinkHistory:
    """原 MAC 全集已经成立后，严格解释一个 prepared 或 prepared→决定序列。"""
    check()
    if len(events) not in {1, 2} or stream.count != len(events):
        raise link_history_changed()
    first = events[0]
    if first[:3] != (row[0], 0, "prepared") or type(first[-1]) is not str:
        raise link_history_changed()
    body = first[-1].encode("utf-8", "strict")
    prepared = decode_product_git_prepared_link(body, checkpoint=check)
    columns = prepared_link_columns(prepared, body)
    claims, core = stream.first, prepared.plan.core
    route_id = prepared.plan.route.execution.plan_id
    if (
        claims.record_kind,
        claims.record_id,
        claims.route_id,
        claims.delivery_id,
        claims.thread_id,
        claims.turn_id,
        claims.call_id,
    ) != (
        "product_link",
        route_id,
        route_id,
        core.delivery_id,
        core.thread_id,
        core.turn_id,
        core.call.call_id,
    ):
        raise link_history_changed()
    if len(events) == 1:
        if row != columns:
            raise link_history_changed()
        return GitLinkHistory(prepared)
    last = events[1]
    if last[:2] != (row[0], 1) or type(last[-1]) is not str:
        raise link_history_changed()
    decision = decode_product_git_decision_link(last[-1].encode("utf-8"), checkpoint=check)
    if (
        decision.plan != prepared.plan
        or decision.approval_request != prepared.approval
        or decision.prepared_body_sha256 != sha256(body).hexdigest()
        or last[2] != decision.phase
        or row != (*columns[:8], decision.phase, decision.sequence, last[-1])
    ):
        raise link_history_changed()
    check()
    return GitLinkHistory(prepared, decision)


def read_git_link_history_rows(
    database: sqlite3.Connection,
    publication: SessionPublicationBinding,
    *,
    checkpoint: Callable[[], None],
) -> tuple[tuple[GitLinkHistory, ...], GitPrefixRows]:
    """先验证所有记录 MAC 与完整尾锚，再解释全部关联，不签旧行或跳过非目标。"""
    catalog = read_git_prefix_catalog(
        database, publication.git_verifier, publication.git_prefix_verifier, checkpoint=checkpoint
    )
    rows = capture_git_prefix_rows(database, checkpoint=checkpoint)
    if rows.tables != catalog.tables:
        raise link_history_changed()
    if any(rows.table(table) for table in _UNIMPLEMENTED_TABLES):
        raise KernelError("git_decision_link_scope_unsupported", "Git账本超出决定关联回读范围")
    streams = {
        (stream.first.record_kind, str(stream.first.record_id)): stream
        for stream in catalog.streams
    }
    events: dict[str, list[Row]] = {}
    for event in rows.table("git_product_link_events"):
        checkpoint()
        if type(event[0]) is not str:
            raise link_history_changed()
        events.setdefault(event[0], []).append(event)
    result = []
    for row in rows.table("git_product_links"):
        checkpoint()
        stream = streams.get(("product_link", str(row[0])))
        if stream is None:
            raise link_history_changed()
        result.append(_read_stream(row, tuple(events.pop(str(row[0]), ())), stream, checkpoint))
    if events or len(streams) != len(result):
        raise link_history_changed()
    checkpoint()
    return tuple(result), rows
