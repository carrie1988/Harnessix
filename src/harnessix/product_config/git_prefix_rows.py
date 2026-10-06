"""GitDB固定表的有界完整观察；不解析未认证payload，不写入或修复数据库。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store_schema_v2 import verify_git_store_v2_schema
from harnessix.product_config.git_prefix_catalog import GIT_PREFIX_TABLES, GitPrefixTable
from harnessix.product_config.git_prefix_sql import (
    git_prefix_transaction_epoch,
    require_git_prefix_sql_window,
)

type Row = tuple[str | int | bytes | None, ...]
type Rows = tuple[tuple[str, tuple[Row, ...]], ...]


@dataclass(frozen=True, slots=True)
class GitPrefixRows:
    """调用者已有SQLite事务内的完整私有行；repr不包含正文。"""

    rows: Rows = field(repr=False)
    tables: tuple[GitPrefixTable, ...]

    def table(self, name: str) -> tuple[Row, ...]:
        """仅查询已经冻结的有限表，不执行调用方SQL。"""
        return dict(self.rows)[name]


def row_bytes(row: Row) -> bytes:
    """带原类型标签的唯一行编码，避免TEXT/BLOB、NULL/空值混淆。"""
    tagged: list[JsonValue] = []
    for value in row:
        if type(value) is bytes:
            tagged.append(["blob", value.hex()])
        elif type(value) is str:
            tagged.append(["text", value])
        elif type(value) is int:
            tagged.append(["integer", value])
        elif value is None:
            tagged.append(["null", None])
        else:
            raise KernelError("publication_history_unproven", "Git记录原始类型无效")
    return json.dumps(tagged, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _table_rows(
    database: sqlite3.Connection, name: str, checkpoint: Callable[[], None], budget: list[int]
) -> tuple[tuple[Row, ...], GitPrefixTable]:
    """游标逐行读取全部列；固定主键排序不接受模型SQL或对正文排序。"""
    checkpoint()
    columns = database.execute(f'PRAGMA main.table_info("{name}")').fetchall()
    names = [f'"{column[1]}"' for column in columns]
    primary = [f'"{column[1]}"' for column in sorted(columns, key=lambda c: c[5]) if column[5]]
    oversized = [f"length(CAST({column} AS BLOB))>524288" for column in names]
    # 超限正文在SQLite中拒绝，而不是先把完整64MiB TEXT分配到Python再检查。
    selection = [
        f"CASE WHEN {size} THEN NULL ELSE {column} END"
        for column, size in zip(names, oversized, strict=True)
    ]
    marker = " OR ".join(oversized)
    cursor = database.execute(
        f"SELECT {','.join(selection)}, CASE WHEN {marker} THEN 1 ELSE 0 END "
        f'FROM main."{name}" ORDER BY {",".join(primary)}'
    )
    rows: list[Row] = []
    digest = hashlib.sha256()
    for raw in cursor:
        checkpoint()
        if raw[-1] or len(rows) >= 100_000:
            raise KernelError("git_prefix_catalog_limit", "Git认证目录超出行数上限")
        row = raw[:-1]
        for value in row:
            if type(value) in {str, bytes}:
                budget[0] += len(value.encode("utf-8")) if type(value) is str else len(value)
        if budget[0] > 32 * 1024 * 1024:
            raise KernelError("git_prefix_catalog_limit", "Git认证完整捕获超出上限")
        encoded = row_bytes(row)
        # 长度分帧；不能通过拼接两行字节模拟另一张表。
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        rows.append(row)
    return tuple(rows), GitPrefixTable(table=name, rows=len(rows), sha256=digest.hexdigest())


def capture_git_prefix_rows(
    database: sqlite3.Connection, *, checkpoint: Callable[[], None]
) -> GitPrefixRows:
    """只在调用者已有事务中捕获，保留取消/期限异常及原有固定容量限制。"""
    checkpoint()
    require_git_prefix_sql_window(database)
    epoch, changes = git_prefix_transaction_epoch(database), database.total_changes
    if not database.in_transaction:
        raise KernelError("git_delivery_store_transaction_required", "Git目录读取需要已有事务")
    verify_git_store_v2_schema(database)
    rows = []
    tables = []
    budget = [0]
    for name in GIT_PREFIX_TABLES:
        values, summary = _table_rows(database, name, checkpoint, budget)
        rows.append((name, values))
        tables.append(summary)
    checkpoint()
    if git_prefix_transaction_epoch(database) != epoch or database.total_changes != changes:
        raise KernelError("publication_history_unproven", "Git捕获窗口中发生了数据或事务变化")
    return GitPrefixRows(tuple(rows), tuple(tables))
