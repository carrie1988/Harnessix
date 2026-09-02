from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import aiosqlite
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, EventDraft, Thread
from harnessix.agent.reducer import apply_event, replay
from harnessix.session.errors import storage_errors

_APPLICATION_ID = 0x4858534B


class SQLiteSessionStore:
    """事件与聚合投影原子提交；多连接 CAS，单 Runtime 宿主。"""

    def __init__(self, path: str | Path, *, fault: Callable[[str], None] | None = None) -> None:
        self.path = Path(path).resolve()
        self._fault = fault or (lambda _: None)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        with storage_errors():
            async with aiosqlite.connect(self.path) as database:
                database.row_factory = aiosqlite.Row
                await database.execute("PRAGMA foreign_keys = ON")
                await database.execute("PRAGMA busy_timeout = 5000")
                await database.execute("PRAGMA synchronous = FULL")
                try:
                    yield database
                except BaseException:
                    await database.rollback()
                    raise

    async def initialize(self) -> None:
        with storage_errors():
            await self._initialize()

    async def _initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            cursor = await database.execute("PRAGMA application_id")
            row = await cursor.fetchone()
            assert row is not None
            app_id = row[0]
            if app_id not in (0, _APPLICATION_ID):
                raise KernelError("wrong_database", "该文件不是 Harnessix Session 数据库")
            if app_id == 0:
                cursor = await database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
                if await cursor.fetchone() is not None:
                    raise KernelError("wrong_database", "不能在其他应用数据库中初始化 Session")
            await database.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            await database.execute(
                "CREATE TABLE IF NOT EXISTS agent_migrations "
                "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
            )
            cursor = await database.execute("SELECT version, checksum FROM agent_migrations")
            applied = {row["version"]: row["checksum"] for row in await cursor.fetchall()}
            root = files("harnessix.session.migrations")
            migrations = sorted(
                (m for m in root.iterdir() if m.name.endswith(".sql")), key=lambda m: m.name
            )
            versions = {int(m.name.split("_", 1)[0]) for m in migrations}
            if set(applied) - versions:
                raise KernelError("schema_too_new", "Session Schema 高于当前程序支持版本")
            if list(sorted(applied)) != list(range(1, len(applied) + 1)):
                raise KernelError("invalid_migration", "Session Migration 历史存在缺口")
            for migration in migrations:
                version = int(migration.name.split("_", 1)[0])
                sql = migration.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if version in applied:
                    if applied[version] != checksum:
                        raise KernelError("migration_changed", "已应用的 Migration 内容发生变化")
                    continue
                # 资源仅允许普通 DDL；避免 executescript 隐式提交破坏迁移原子性。
                for statement in sql.split(";"):
                    if statement.strip():
                        await database.execute(statement)
                await database.execute(
                    "INSERT INTO agent_migrations VALUES (?, ?)", (version, checksum)
                )
            cursor = await database.execute("PRAGMA quick_check")
            row = await cursor.fetchone()
            assert row is not None
            if row[0] != "ok":
                raise KernelError("database_corrupt", "Session 数据库完整性检查失败")
            await database.commit()
            self.path.chmod(0o600)
            await database.execute("PRAGMA journal_mode = WAL")

    @asynccontextmanager
    async def runtime_owner(self) -> AsyncIterator[None]:
        """本地 macOS/Linux 宿主锁；进程退出由 OS 释放，禁止第二宿主接管活跃 Turn。"""
        with storage_errors():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                str(self.path) + ".runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise KernelError(
                        "runtime_busy", "该 Session 数据库已有活跃 Runtime 宿主"
                    ) from exc
                yield
            finally:
                os.close(descriptor)

    async def _snapshot(self, database: aiosqlite.Connection, thread_id: UUID) -> Thread | None:
        cursor = await database.execute(
            "SELECT * FROM agent_threads WHERE thread_id = ?", (str(thread_id),)
        )
        row = await cursor.fetchone()
        cursor = await database.execute(
            "SELECT COALESCE(MAX(sequence), 0), COUNT(*) FROM agent_events WHERE thread_id = ?",
            (str(thread_id),),
        )
        last_row = await cursor.fetchone()
        assert last_row is not None
        last = last_row[0]
        if last != last_row[1]:
            raise KernelError("event_corrupt", "事件日志存在序号缺口")
        if row is None:
            if last:
                raise KernelError("projection_missing", "投影缺失，请从事件日志重建")
            return None
        encoded: str = row["snapshot_json"]
        if row["projection_version"] not in (1, 2, 3, 4):
            raise KernelError("projection_too_new", "Session 投影版本高于当前程序支持版本")
        if hashlib.sha256(encoded.encode()).hexdigest() != row["snapshot_sha256"]:
            raise KernelError("projection_corrupt", "快照校验失败，请重建投影")
        try:
            thread = Thread.model_validate_json(encoded)
        except ValidationError:
            raise KernelError("projection_corrupt", "快照结构损坏，请重建投影") from None
        if (
            thread.thread_id != thread_id
            or thread.sequence != row["sequence"]
            or thread.sequence != last
        ):
            raise KernelError("projection_corrupt", "投影序号与事件日志不一致")
        return thread

    async def get_thread(self, thread_id: UUID) -> Thread:
        async with self._connection() as database:
            await database.execute("BEGIN")
            thread = await self._snapshot(database, thread_id)
            if thread is None:
                raise KernelError("thread_not_found", "Thread 不存在")
            return thread

    async def thread_ids(self) -> list[UUID]:
        async with self._connection() as database:
            cursor = await database.execute(
                "SELECT thread_id FROM agent_events UNION "
                "SELECT thread_id FROM agent_threads ORDER BY thread_id"
            )
            try:
                return [UUID(row[0]) for row in await cursor.fetchall()]
            except ValueError:
                raise KernelError("event_corrupt", "Thread 索引包含无效标识") from None

    async def _save(self, database: aiosqlite.Connection, thread: Thread) -> None:
        encoded = thread.model_dump_json()
        await database.execute(
            "INSERT INTO agent_threads "
            "(thread_id, sequence, snapshot_json, snapshot_sha256, projection_version) "
            "VALUES (?, ?, ?, ?, 4) "
            "ON CONFLICT(thread_id) DO UPDATE SET sequence = excluded.sequence, "
            "snapshot_json = excluded.snapshot_json, snapshot_sha256 = excluded.snapshot_sha256, "
            "projection_version = excluded.projection_version",
            (
                str(thread.thread_id),
                thread.sequence,
                encoded,
                hashlib.sha256(encoded.encode()).hexdigest(),
            ),
        )

    async def append(
        self,
        thread_id: UUID,
        drafts: Sequence[EventDraft],
        *,
        expected_sequence: int,
    ) -> Thread:
        # 先做值拷贝，冻结调用方可能持有的嵌套 arguments。
        try:
            batch = tuple(
                EventDraft.model_validate_json(d.model_dump_json(warnings="error")) for d in drafts
            )
        except ValueError:
            raise KernelError("invalid_batch", "事件批次不符合契约") from None
        if not batch or len({d.event_id for d in batch}) != len(batch):
            raise KernelError("invalid_batch", "事件批次为空或包含重复 ID")
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            matched: list[AgentEvent] = []
            for draft in batch:
                cursor = await database.execute(
                    "SELECT * FROM agent_events WHERE event_id = ?",
                    (str(draft.event_id),),
                )
                row = await cursor.fetchone()
                if row is not None:
                    event = self._parse_event(row)
                    stored = EventDraft.model_validate(
                        event.model_dump(exclude={"thread_id", "sequence"})
                    )
                    if stored != draft or event.thread_id != thread_id:
                        raise KernelError("event_conflict", "同一事件 ID 已绑定不同载荷")
                    matched.append(event)
            if matched:
                if len(matched) != len(batch) or any(
                    event.sequence != expected_sequence + index
                    for index, event in enumerate(matched, 1)
                ):
                    raise KernelError("event_conflict", "事件批次部分重复或顺序冲突")
                thread = await self._snapshot(database, thread_id)
                assert thread is not None
                return thread
            thread = await self._snapshot(database, thread_id)
            sequence = thread.sequence if thread else 0
            if sequence != expected_sequence:
                raise KernelError("sequence_conflict", "Thread 已更新，请重新读取 sequence")
            for draft in batch:
                sequence += 1
                event = AgentEvent(**draft.model_dump(), thread_id=thread_id, sequence=sequence)
                thread = apply_event(thread, event)
                await database.execute(
                    "INSERT INTO agent_events VALUES (?, ?, ?, ?)",
                    (str(thread_id), sequence, str(event.event_id), event.model_dump_json()),
                )
            assert thread is not None
            self._fault("session.after_events")
            await self._save(database, thread)
            self._fault("session.after_projection")
            await database.commit()
            self._fault("session.after_commit")
            return thread

    @staticmethod
    def _parse_event(row: aiosqlite.Row) -> AgentEvent:
        try:
            event = AgentEvent.model_validate_json(row["event_json"])
        except ValidationError:
            raise KernelError("event_corrupt", "事件结构损坏或版本不支持") from None
        if (
            str(event.thread_id) != row["thread_id"]
            or event.sequence != row["sequence"]
            or str(event.event_id) != row["event_id"]
        ):
            raise KernelError("event_corrupt", "事件载荷与索引不一致")
        return event

    async def _events(
        self, database: aiosqlite.Connection, thread_id: UUID, after: int
    ) -> list[AgentEvent]:
        cursor = await database.execute(
            "SELECT * FROM agent_events WHERE thread_id = ? AND sequence > ? ORDER BY sequence",
            (str(thread_id), after),
        )
        events = []
        for expected, row in enumerate(await cursor.fetchall(), after + 1):
            event = self._parse_event(row)
            if row["sequence"] != expected:
                raise KernelError("event_corrupt", "事件日志存在序号缺口")
            events.append(event)
        return events

    async def events(self, thread_id: UUID, *, after: int = 0) -> list[AgentEvent]:
        if after < 0:
            raise KernelError("invalid_cursor", "事件游标不能为负数")
        async with self._connection() as database:
            return await self._events(database, thread_id, after)

    async def rebuild(self, thread_id: UUID) -> Thread:
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            thread = replay(await self._events(database, thread_id, 0))
            await self._save(database, thread)
            await database.commit()
            return thread
