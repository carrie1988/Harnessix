"""GitDB v2唯一结构合同；不迁移、认证或装配产品Writer，不改变默认v1。"""

from __future__ import annotations

import sqlite3
from hashlib import sha256

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store_schema import _SCHEMA_SQL as _V1_SQL

GIT_PRODUCT_LINK_PHASES = (
    "prepared",
    "approved",
    "materials_ready",
    "anchor_intent",
    "anchor_ready",
    "native_patch_intent",
    "native_patch_prepared",
    "native_bridge_closed",
    "delivery_worktree_intent",
    "delivery_worktree_ready",
    "checkpoint_intent",
    "checkpoint_closed",
    "commit_intent",
    "commit_closed",
    "result_closed",
    "unknown",
    "diverged",
    "failed",
)
_MAX_INTEGER = 2**63 - 1
_MAX_BODY_BYTES = 64 * 1024 * 1024
_LINK_PHASE_CHECK = "phase IN (" + ",".join(f"'{p}'" for p in GIT_PRODUCT_LINK_PHASES) + ")"


def _uuid(name: str, suffix: str = "") -> str:
    """SQL只固定UUID文本长度；实际UUID语法仍由正式领域边界验证。"""
    return f"{name} TEXT NOT NULL {suffix} CHECK(length({name})=36)"


def _digest(name: str) -> str:
    """固定小写64位十六进制，不把普通SHA列视作认证。"""
    return f"{name} TEXT NOT NULL CHECK(length({name})=64 AND {name} NOT GLOB '*[^0-9a-f]*')"


def _integer(name: str, minimum: int = 0, maximum: int = _MAX_INTEGER) -> str:
    return f"{name} INTEGER NOT NULL CHECK({name} BETWEEN {minimum} AND {maximum})"


_PAYLOAD = f"payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<={_MAX_BODY_BYTES})"
_SEAL = "seal BLOB NOT NULL CHECK(length(seal) BETWEEN 1 AND 4096)"
_NEW_SQL = f"""
CREATE TABLE git_product_links (
    {_uuid("route_id", "PRIMARY KEY")},
    {_uuid("delivery_id", "UNIQUE")},
    {_uuid("thread_id")},
    {_uuid("turn_id")},
    {_uuid("call_id")},
    action_kind TEXT NOT NULL CHECK(action_kind IN ('checkpoint','commit')),
    {_digest("core_sha256")},
    {_digest("route_fingerprint")},
    phase TEXT NOT NULL CHECK({_LINK_PHASE_CHECK}),
    {_integer("sequence")},
    {_PAYLOAD},
    UNIQUE(thread_id,turn_id,call_id),
    UNIQUE(delivery_id,route_id),
    UNIQUE(delivery_id,route_id,thread_id,turn_id,call_id)
) STRICT;
CREATE TABLE git_product_link_events (
    {_uuid("route_id")},
    {_integer("sequence")},
    phase TEXT NOT NULL CHECK({_LINK_PHASE_CHECK}),
    {_PAYLOAD},
    PRIMARY KEY(route_id,sequence),
    FOREIGN KEY(route_id) REFERENCES git_product_links(route_id)
) STRICT;
CREATE TABLE git_native_bridge_index (
    {_uuid("bridge_id", "PRIMARY KEY")},
    {_uuid("transaction_id", "UNIQUE")},
    {_uuid("route_id")},
    {_uuid("anchor_id")},
    {_uuid("worktree_id")},
    {_digest("bridge_sha256")},
    FOREIGN KEY(route_id) REFERENCES git_product_links(route_id)
) STRICT;
CREATE TABLE git_object_inventories (
    {_uuid("inventory_id", "PRIMARY KEY")},
    {_uuid("delivery_id")},
    {_uuid("route_id")},
    phase TEXT NOT NULL CHECK(phase IN ('materials_ready','effect_closed')),
    {_integer("sequence")},
    {_digest("inventory_sha256")},
    {_digest("scope_sha256")},
    {_PAYLOAD},
    FOREIGN KEY(delivery_id,route_id) REFERENCES git_product_links(delivery_id,route_id)
) STRICT;
CREATE TABLE git_object_inventory_events (
    {_uuid("inventory_id")},
    {_integer("sequence")},
    phase TEXT NOT NULL CHECK(phase IN ('materials_ready','effect_closed')),
    {_PAYLOAD},
    PRIMARY KEY(inventory_id,sequence),
    FOREIGN KEY(inventory_id) REFERENCES git_object_inventories(inventory_id)
) STRICT;
CREATE TABLE git_record_publications (
    record_kind TEXT NOT NULL CHECK(record_kind IN
        ('object_inventory','product_link','worktree_event','checkpoint','commit_event')),
    {_uuid("record_id")},
    {_uuid("publication_epoch")},
    {_integer("sequence", 1)},
    {_uuid("delivery_id")},
    {_uuid("thread_id")},
    {_uuid("turn_id")},
    {_uuid("call_id")},
    {_uuid("route_id")},
    {_digest("previous_prefix")},
    {_digest("body_sha256")},
    {_integer("body_bytes", 1, _MAX_BODY_BYTES)},
    {_digest("prefix_sha256")},
    {_SEAL},
    PRIMARY KEY(record_kind,record_id,publication_epoch,sequence),
    FOREIGN KEY(delivery_id,route_id,thread_id,turn_id,call_id)
        REFERENCES git_product_links(delivery_id,route_id,thread_id,turn_id,call_id),
    CHECK(record_kind!='product_link' OR record_id=route_id)
) STRICT;
CREATE TABLE git_prefix_anchor (
    singleton INT NOT NULL PRIMARY KEY CHECK(singleton=1),
    {_integer("revision")},
    {_uuid("genesis_epoch")},
    {_integer("body_bytes", 1, _MAX_BODY_BYTES)},
    {_digest("body_sha256")},
    {_PAYLOAD},
    {_SEAL},
    CHECK(body_bytes=length(CAST(payload AS BLOB)))
) STRICT;
"""


def _statements(sql: str) -> tuple[str, ...]:
    return tuple(" ".join(part.split()) for part in sql.split(";") if part.strip())


_V1_STATEMENTS = _statements(_V1_SQL.replace("IF NOT EXISTS ", ""))
_NEW_STATEMENTS = _statements(_NEW_SQL)
# 校验和输入：13条DDL按既有六表、上述七表顺序；每条单行空白规范化，分号与LF结尾。
GIT_STORE_V2_DDL = "".join(statement + ";\n" for statement in (*_V1_STATEMENTS, *_NEW_STATEMENTS))
GIT_STORE_V2_MIGRATION_CHECKSUM = sha256(GIT_STORE_V2_DDL.encode("ascii")).hexdigest()
_AUTO_INDEX_COUNTS = (
    1,
    2,
    1,
    3,
    3,
    1,  # 原六表的PK及UNIQUE，原定义不改。
    5,
    1,
    2,
    1,
    1,
    1,
    1,  # INT PK避免INTEGER rowid别名将NULL隐式生成成1。
)
type _Schema = dict[str, tuple[str, str, str | None]]


def _expected(statements: tuple[str, ...]) -> _Schema:
    result: _Schema = {}
    for statement, count in zip(statements, _AUTO_INDEX_COUNTS, strict=False):
        name = statement.split()[2]
        result[name] = ("table", name, statement)
        for index in range(1, count + 1):
            result[f"sqlite_autoindex_{name}_{index}"] = ("index", name, None)
    return result


_V1_SCHEMA = _expected(_V1_STATEMENTS)
_V2_SCHEMA = _expected((*_V1_STATEMENTS, *_NEW_STATEMENTS))


def _corrupt() -> KernelError:
    return KernelError("git_delivery_store_corrupt", "Git交付账本结构不一致")


def _observe(database: sqlite3.Connection) -> tuple[str, _Schema]:
    """先验证完整结构，再读已知真实metadata表；拒绝前不执行变形VIEW。"""
    try:
        if database.execute("SELECT name FROM temp.sqlite_schema LIMIT 1").fetchone() is not None:
            raise _corrupt()
        entries = database.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema ORDER BY name LIMIT ?",
            (len(_V2_SCHEMA) + 1,),
        ).fetchall()
        schema = {
            name: (kind, table, None if sql is None else " ".join(sql.split()))
            for kind, name, table, sql in entries
        }
        if len(schema) != len(entries) or schema not in (_V1_SCHEMA, _V2_SCHEMA):
            raise _corrupt()
        if database.execute("PRAGMA main.encoding").fetchone() != ("UTF-8",):
            # CAST(TEXT AS BLOB)按数据库编码计量，不能以UTF-16放大UTF-8业务边界。
            raise _corrupt()
        row = database.execute(
            "SELECT value FROM main.git_delivery_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise _corrupt()
        if row not in (("1",), ("2",)):
            raise KernelError("git_delivery_store_version", "Git交付存储版本不受支持")
        return row[0], schema
    except sqlite3.Error:
        raise _corrupt() from None


def _create_git_store_v2_tables(database: sqlite3.Connection) -> None:
    """仅在调用者已有事务中建七表；不修改版本行、不修复部分结构或控制事务。"""
    if not database.in_transaction:
        raise KernelError("git_delivery_store_transaction_required", "Git结构创建需要已有事务")
    version, actual = _observe(database)
    if actual == _V2_SCHEMA:
        return
    if version != "1" or actual != _V1_SCHEMA:
        raise _corrupt()
    try:
        for statement in _NEW_STATEMENTS:
            database.execute(statement)
        if _observe(database)[1] != _V2_SCHEMA:
            raise _corrupt()
    except sqlite3.Error:
        # 事务归调用者；失败不提交，不自行回滚其他尚未提交的操作。
        raise _corrupt() from None


def verify_git_store_v2_schema(database: sqlite3.Connection) -> None:
    """精确只读核验版本2及13表DDL/内部索引，不证明业务、认证或迁移完成。"""
    version, actual = _observe(database)
    if version != "2":
        raise KernelError("git_delivery_store_version", "Git交付存储版本不受支持")
    if actual != _V2_SCHEMA:
        raise _corrupt()
