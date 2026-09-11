"""以SQLite保存不可变MCP目录、连接快照和Hash链连接事件。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Collection
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.mcp.contracts import (
    McpCatalogSnapshot,
    McpConnectionEvent,
    McpConnectionSnapshot,
    McpConnectionState,
    build_mcp_connection_event,
)

_SCHEMA_VERSION = "1"


class SQLiteMcpStore:
    """MCP目录不可变快照、连接投影和Hash链事件。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._closed = False
        self._prepare_parent()
        self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if os.name == "posix":
                self._path.chmod(0o600)
            self._initialize()
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def _prepare_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self._path.parent.chmod(0o700)

    def _initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS mcp_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM mcp_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO mcp_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("mcp_store_version", "MCP存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS mcp_catalog_snapshots (
                server_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                catalog_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(server_id, generation)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS mcp_connection_snapshots (
                server_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                catalog_digest TEXT,
                last_event_digest TEXT NOT NULL,
                error_code TEXT,
                updated_at TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS mcp_connection_events (
                server_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(server_id, sequence),
                UNIQUE(digest)
            ) STRICT;
            """
        )

    def begin_connect(self, server_id: str) -> McpConnectionSnapshot:
        current = self._load_optional(server_id)
        if current is None:
            return self._transition(
                server_id,
                expected=(),
                target="connecting",
                catalog=None,
                error_code=None,
            )
        return self._transition(
            server_id,
            expected={"failed", "closed", "schema_changed"},
            target="connecting",
            catalog=None,
            error_code=None,
        )

    def next_generation(self, server_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM mcp_catalog_snapshots WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if row is None or type(row[0]) is not int:
            raise KernelError("mcp_store_corrupt", "MCP目录代次索引损坏")
        return row[0] + 1

    def connected(self, catalog: McpCatalogSnapshot) -> McpConnectionSnapshot:
        return self._transition(
            catalog.server.server_id,
            expected={"connecting"},
            target="connected",
            catalog=catalog,
            error_code=None,
        )

    def schema_changed(self, catalog: McpCatalogSnapshot) -> McpConnectionSnapshot:
        return self._transition(
            catalog.server.server_id,
            expected={"connected"},
            target="schema_changed",
            catalog=catalog,
            error_code="mcp_tool_schema_changed",
        )

    def failed(self, server_id: str, error_code: str) -> McpConnectionSnapshot:
        return self._transition(
            server_id,
            expected={"connecting", "connected", "schema_changed", "failed"},
            target="failed",
            catalog=None,
            error_code=error_code,
        )

    def closed(self, server_id: str) -> McpConnectionSnapshot:
        return self._transition(
            server_id,
            expected={"connected", "schema_changed", "failed"},
            target="closed",
            catalog=None,
            error_code=None,
        )

    def recover_interrupted(self) -> tuple[str, ...]:
        rows = self._db.execute(
            "SELECT server_id FROM mcp_connection_snapshots "
            "WHERE state IN ('connecting', 'connected') ORDER BY server_id"
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            if not isinstance(row[0], str):
                raise KernelError("mcp_store_corrupt", "MCP连接索引损坏")
            self.failed(row[0], "mcp_host_interrupted")
            recovered.append(row[0])
        return tuple(recovered)

    def load(self, server_id: str) -> McpConnectionSnapshot:
        snapshot = self._load_optional(server_id)
        if snapshot is None:
            raise KernelError("mcp_connection_not_found", "MCP连接不存在")
        return snapshot

    def catalog(self, server_id: str, generation: int | None = None) -> McpCatalogSnapshot:
        selected = generation
        if selected is None:
            current = self.load(server_id)
            if current.generation == 0:
                raise KernelError("mcp_catalog_not_found", "MCP连接尚无目录快照")
            selected = current.generation
        row = self._db.execute(
            "SELECT catalog_digest, payload FROM mcp_catalog_snapshots "
            "WHERE server_id = ? AND generation = ?",
            (server_id, selected),
        ).fetchone()
        if row is None:
            raise KernelError("mcp_catalog_not_found", "MCP目录快照不存在")
        try:
            catalog = McpCatalogSnapshot.model_validate_json(row[1])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("mcp_store_corrupt", "MCP目录快照损坏") from None
        if (
            catalog.server.server_id != server_id
            or catalog.generation != selected
            or catalog.catalog_sha256 != row[0]
        ):
            raise KernelError("mcp_store_corrupt", "MCP目录索引与正文不一致")
        return catalog

    def events(self, server_id: str) -> tuple[McpConnectionEvent, ...]:
        current = self.load(server_id)
        rows = self._db.execute(
            "SELECT sequence, digest, payload FROM mcp_connection_events "
            "WHERE server_id = ? ORDER BY sequence",
            (server_id,),
        ).fetchall()
        try:
            events = tuple(McpConnectionEvent.model_validate_json(row[2]) for row in rows)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("mcp_store_corrupt", "MCP连接事件损坏") from None
        previous: str | None = None
        state: McpConnectionState | None = None
        for index, (row, event) in enumerate(zip(rows, events, strict=True), start=1):
            if (
                row[0] != index
                or row[1] != event.digest
                or event.server_id != server_id
                or event.sequence != index
                or event.previous_digest != previous
                or event.from_state != state
            ):
                raise KernelError("mcp_store_corrupt", "MCP连接事件链损坏")
            previous = event.digest
            state = event.to_state
        if (
            len(events) != current.sequence
            or previous != current.last_event_digest
            or state != current.state
        ):
            raise KernelError("mcp_store_corrupt", "MCP连接事件链与投影不一致")
        return events

    def _load_optional(self, server_id: str) -> McpConnectionSnapshot | None:
        row = self._db.execute(
            "SELECT state, sequence, generation, catalog_digest, last_event_digest, "
            "error_code, updated_at FROM mcp_connection_snapshots WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            snapshot = McpConnectionSnapshot(
                server_id=server_id,
                state=row[0],
                sequence=row[1],
                generation=row[2],
                catalog_sha256=row[3],
                last_event_digest=row[4],
                error_code=row[5],
                updated_at=datetime.fromisoformat(row[6]),
            )
            event_row = self._db.execute(
                "SELECT digest, payload FROM mcp_connection_events "
                "WHERE server_id = ? AND sequence = ?",
                (server_id, snapshot.sequence),
            ).fetchone()
            if event_row is None:
                raise ValueError
            event = McpConnectionEvent.model_validate_json(event_row[1])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("mcp_store_corrupt", "MCP连接投影损坏") from None
        if (
            event_row[0] != snapshot.last_event_digest
            or event.digest != snapshot.last_event_digest
            or event.to_state != snapshot.state
            or event.sequence != snapshot.sequence
            or event.catalog_sha256 != snapshot.catalog_sha256
        ):
            raise KernelError("mcp_store_corrupt", "MCP连接投影与事件不一致")
        if snapshot.generation:
            catalog = self.catalog(server_id, snapshot.generation)
            if catalog.catalog_sha256 != snapshot.catalog_sha256:
                raise KernelError("mcp_store_corrupt", "MCP连接目录绑定不一致")
        return snapshot

    def _transition(
        self,
        server_id: str,
        *,
        expected: Collection[McpConnectionState],
        target: McpConnectionState,
        catalog: McpCatalogSnapshot | None,
        error_code: str | None,
    ) -> McpConnectionSnapshot:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            current = self._load_optional(server_id)
            if current is None:
                if expected or target != "connecting":
                    raise KernelError("mcp_connection_state_conflict", "MCP连接状态冲突")
                sequence = 1
                previous_digest = None
                generation = 0
                catalog_digest = None
                from_state = None
            else:
                if current.state not in expected:
                    raise KernelError("mcp_connection_state_conflict", "MCP连接状态冲突")
                sequence = current.sequence + 1
                previous_digest = current.last_event_digest
                generation = current.generation
                catalog_digest = current.catalog_sha256
                from_state = current.state
            if catalog is not None:
                if (
                    catalog.server.server_id != server_id
                    or catalog.generation != self.next_generation(server_id)
                ):
                    raise KernelError("mcp_catalog_conflict", "MCP目录代次与连接不一致")
                self._db.execute(
                    "INSERT INTO mcp_catalog_snapshots VALUES (?, ?, ?, ?)",
                    (
                        server_id,
                        catalog.generation,
                        catalog.catalog_sha256,
                        catalog.model_dump_json(warnings="error"),
                    ),
                )
                generation = catalog.generation
                catalog_digest = catalog.catalog_sha256
            now = utc_now()
            event = build_mcp_connection_event(
                server_id=server_id,
                sequence=sequence,
                from_state=from_state,
                to_state=target,
                previous_digest=previous_digest,
                occurred_at=now,
                catalog_sha256=catalog_digest,
                error_code=error_code,
            )
            values = (
                target,
                sequence,
                generation,
                catalog_digest,
                event.digest,
                error_code,
                now.isoformat(),
            )
            if current is None:
                self._db.execute(
                    "INSERT INTO mcp_connection_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (server_id, *values),
                )
            else:
                changed = self._db.execute(
                    "UPDATE mcp_connection_snapshots SET state = ?, sequence = ?, "
                    "generation = ?, catalog_digest = ?, last_event_digest = ?, error_code = ?, "
                    "updated_at = ? WHERE server_id = ? AND sequence = ? AND last_event_digest = ?",
                    (*values, server_id, current.sequence, current.last_event_digest),
                )
                if changed.rowcount != 1:
                    raise KernelError("mcp_connection_state_conflict", "MCP连接并发更新冲突")
            self._db.execute(
                "INSERT INTO mcp_connection_events VALUES (?, ?, ?, ?)",
                (server_id, sequence, event.digest, event.model_dump_json(warnings="error")),
            )
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError("mcp_catalog_conflict", "MCP目录或连接事件发生冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.load(server_id)

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteMcpStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
