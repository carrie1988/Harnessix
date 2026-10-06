"""有限五kind的完整物理事件覆盖与认证链核验；不解释业务批准或对象图。"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_prefix_catalog import GitPrefixStream
from harnessix.product_config.git_prefix_rows import GitPrefixRows, Row
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from harnessix.session.store_publication import GitPublicationVerifier, extend_prefix

EVENT_TABLES = (
    ("product_link", "git_product_link_events", "git_product_links", 9),
    ("object_inventory", "git_object_inventory_events", "git_object_inventories", 4),
    ("worktree_event", "git_worktree_events", "git_worktrees", 4),
    ("commit_event", "git_commit_events", "git_commits", 5),
)
type RecordKey = tuple[str, str, int]
type Bodies = dict[RecordKey, bytes]


def unproven() -> KernelError:
    """固定拒绝，不输出私有payload、路径、对象或底层异常。"""
    return KernelError("publication_history_unproven", "Git完整认证历史无法核验")


def _body(value: object) -> bytes:
    if type(value) is not str:
        raise unproven()
    return value.encode("utf-8", "strict")


def _event_bodies(rows: GitPrefixRows, checkpoint: Callable[[], None]) -> Bodies:
    bodies: Bodies = {}
    for kind, events, current, sequence_column in EVENT_TABLES:
        latest: dict[str, tuple[int, object, bytes]] = {}
        for row in rows.table(events):
            checkpoint()
            identity, sequence = row[:2]
            if type(identity) is not str or type(sequence) is not int or sequence < 0:
                raise unproven()
            prior = latest.get(identity)
            if sequence != (0 if prior is None else prior[0] + 1):
                raise unproven()
            body = _body(row[-1])
            bodies[kind, identity, sequence] = body
            latest[identity] = sequence, row[2], body
        projected = {
            row[0]: (row[sequence_column], row[sequence_column - 1], _body(row[-1]))
            for row in rows.table(current)
        }
        if projected != latest:
            raise unproven()
    for row in rows.table("git_checkpoints"):
        checkpoint()
        bodies["checkpoint", str(row[0]), 0] = _body(row[-1])
    return bodies


def record_bodies(rows: GitPrefixRows, *, checkpoint: Callable[[], None]) -> Bodies:
    """验证物理事件从零连续和完整当前投影；不是原模型状态机的语义验真。"""
    try:
        if rows.table("git_delivery_metadata") != (("schema_version", "2"),):
            raise unproven()
        return _event_bodies(rows, checkpoint)
    except (UnicodeError, ValueError, TypeError, IndexError):
        raise unproven() from None


def claims_from_row(row: Row) -> GitDeliveryRecordClaims:
    """从固定冗余列重建声明；认证来源仍由原Verifier及独立目录决定。"""
    try:
        identities = (1, 2, 4, 5, 6, 7, 8)
        ids = []
        for index in identities:
            value = row[index]
            if type(value) is not str:
                raise unproven()
            ids.append(UUID(value))
        claims = GitDeliveryRecordClaims.model_validate(
            {
                "record_kind": row[0],
                "record_id": ids[0],
                "publication_epoch": ids[1],
                "sequence": row[3],
                "delivery_id": ids[2],
                "thread_id": ids[3],
                "turn_id": ids[4],
                "call_id": ids[5],
                "route_id": ids[6],
                "previous_sha256": row[9],
            }
        )
        encoded = (
            claims.record_kind,
            str(claims.record_id),
            str(claims.publication_epoch),
            claims.sequence,
            str(claims.delivery_id),
            str(claims.thread_id),
            str(claims.turn_id),
            str(claims.call_id),
            str(claims.route_id),
            claims.previous_sha256,
        )
        if row[:10] != encoded:
            raise unproven()
        return claims
    except (ValueError, TypeError, AttributeError):
        raise unproven() from None


def verify_record_streams(
    rows: GitPrefixRows, verifier: GitPublicationVerifier, *, checkpoint: Callable[[], None]
) -> tuple[GitPrefixStream, ...]:
    """逐条原MAC、完整正文、前序及尾前缀核验；拒绝缺失、重复epoch或孤儿。"""
    bodies = record_bodies(rows, checkpoint=checkpoint)
    streams: dict[tuple[str, str], GitPrefixStream] = {}
    covered: set[RecordKey] = set()
    for row in rows.table("git_record_publications"):
        checkpoint()
        claims = claims_from_row(row)
        key = claims.record_kind, str(claims.record_id)
        prior = streams.get(key)
        first = claims if prior is None else prior.first
        expected = first.model_dump()
        expected.update(
            sequence=1 if prior is None else prior.count + 1,
            previous_sha256="0" * 64 if prior is None else prior.prefix_sha256,
        )
        if claims != GitDeliveryRecordClaims.model_validate(expected):
            raise unproven()
        body_key = (*key, claims.sequence - 1)
        if body_key not in bodies or body_key in covered:
            raise unproven()
        body = bodies[body_key]
        verifier.verify(row[13], claims, body, checkpoint=checkpoint)
        seal = row[13]
        if type(seal) is not bytes:
            raise unproven()
        checksum, prefix = sha256(body).hexdigest(), extend_prefix(claims.previous_sha256, seal)
        if row[10:13] != (checksum, len(body), prefix):
            raise unproven()
        streams[key] = GitPrefixStream(
            first=first, count=claims.sequence, last_body_sha256=checksum, prefix_sha256=prefix
        )
        covered.add(body_key)
    if covered != bodies.keys():
        raise unproven()
    checkpoint()
    return tuple(streams[key] for key in sorted(streams))
