"""Session事件存储：以SQLite事务实现对应持久化端口。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import aiosqlite
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.lifecycle import validate_fork_snapshot
from harnessix.agent.models import AgentEvent, EventDraft, Thread, ThreadForked
from harnessix.agent.reducer import apply_event, replay
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.session.errors import storage_errors

_APPLICATION_ID = 0x4858534B
_WAL_TIMEOUT_SECONDS = 5.0


def _open_runtime_owner_lock(path: Path) -> int:
    with storage_errors():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            str(path) + ".runtime.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    try:
        acquire_exclusive_file_lock(descriptor)
    except BlockingIOError as exc:
        _close_runtime_owner_lock(descriptor)
        raise KernelError("runtime_busy", "该 Session 数据库已有活跃 Runtime 宿主") from exc
    except OSError:
        _close_runtime_owner_lock(descriptor)
        raise KernelError("storage_unavailable", "Session 宿主锁获取失败") from None
    return descriptor


def _close_runtime_owner_lock(descriptor: int) -> None:
    with storage_errors():
        os.close(descriptor)


class SQLiteSessionStore:
    """事件与聚合投影原子提交；多连接 CAS，单 Runtime 宿主。"""

    def __init__(self, path: str | Path, *, fault: Callable[[str], None] | None = None) -> None:
        self.path = Path(path).resolve()
        self._fault = fault or (lambda _: None)
        self._runtime_owner_token: object | None = None

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
        # 先释放迁移连接的全部游标/锁，避免与另一初始化连接的模式切换形成锁升级冲突。
        await self._enable_wal()

    async def _enable_wal(self) -> None:
        async with self._connection() as database:
            # 模式切换遇到锁升级冲突时 SQLite 可跳过 busy handler；只重试这一幂等操作。
            await database.execute("PRAGMA busy_timeout = 0")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _WAL_TIMEOUT_SECONDS
            while True:
                try:
                    cursor = await database.execute("PRAGMA journal_mode = WAL")
                    try:
                        row = await cursor.fetchone()
                    finally:
                        await cursor.close()
                    if row is None or row[0] != "wal":
                        raise KernelError("storage_unavailable", "Session 无法启用 WAL 模式")
                    return
                except sqlite3.OperationalError as error:
                    remaining = deadline - loop.time()
                    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                    if code != sqlite3.SQLITE_BUSY or remaining <= 0:
                        raise
                    await asyncio.sleep(min(0.05, remaining))

    @asynccontextmanager
    async def runtime_owner(self) -> AsyncIterator[None]:
        """本地跨平台宿主锁；进程退出由OS释放，禁止第二宿主接管活跃Turn。"""

        descriptor = _open_runtime_owner_lock(self.path)
        try:
            owner = object()
            self._runtime_owner_token = owner
            try:
                yield
            finally:
                if self._runtime_owner_token is owner:
                    self._runtime_owner_token = None
        finally:
            _close_runtime_owner_lock(descriptor)

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
        if row["projection_version"] not in (
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
            19,
            20,
        ):
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
            "VALUES (?, ?, ?, ?, 20) "
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
        batch = self._freeze_batch(drafts)
        if any(isinstance(draft.payload, ThreadForked) for draft in batch):
            raise KernelError("thread_fork_requires_cas", "Fork必须同时校验来源Thread")
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            thread, changed = await self._append_in_transaction(
                database, thread_id, batch, expected_sequence=expected_sequence
            )
            await database.commit()
            if changed:
                self._fault("session.after_commit")
            return thread

    async def fork(
        self,
        source_thread_id: UUID,
        destination_thread_id: UUID,
        draft: EventDraft,
        *,
        expected_source_sequence: int,
    ) -> Thread:
        batch = self._freeze_batch((draft,))
        payload = batch[0].payload
        if (
            not isinstance(payload, ThreadForked)
            or batch[0].turn_id is not None
            or source_thread_id == destination_thread_id
            or payload.snapshot.source_thread_id != source_thread_id
        ):
            raise KernelError("thread_fork_invalid", "Fork创建事件或Thread身份不合法")
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            destination = await self._snapshot(database, destination_thread_id)
            if destination is not None:
                thread, changed = await self._append_in_transaction(
                    database, destination_thread_id, batch, expected_sequence=0
                )
                await database.commit()
                if changed:
                    self._fault("session.after_commit")
                return thread
            source = await self._snapshot(database, source_thread_id)
            if source is None:
                raise KernelError("thread_not_found", "Fork来源Thread不存在")
            if source.sequence != expected_source_sequence:
                raise KernelError("sequence_conflict", "Fork来源Thread已更新")
            if payload.workspace != source.workspace:
                raise KernelError("thread_fork_invalid", "Fork不能改变来源Workspace")
            validate_fork_snapshot(source, payload.snapshot)
            self._fault("session.fork.after_source")
            thread, changed = await self._append_in_transaction(
                database, destination_thread_id, batch, expected_sequence=0
            )
            await database.commit()
            if changed:
                self._fault("session.after_commit")
            return thread

    @staticmethod
    def _freeze_batch(drafts: Sequence[EventDraft]) -> tuple[EventDraft, ...]:
        # 先做值拷贝，冻结调用方可能持有的嵌套 arguments。
        try:
            batch = tuple(
                EventDraft.model_validate_json(d.model_dump_json(warnings="error")) for d in drafts
            )
        except ValueError:
            raise KernelError("invalid_batch", "事件批次不符合契约") from None
        if not batch or len({d.event_id for d in batch}) != len(batch):
            raise KernelError("invalid_batch", "事件批次为空或包含重复 ID")
        return batch

    async def _append_in_transaction(
        self,
        database: aiosqlite.Connection,
        thread_id: UUID,
        batch: tuple[EventDraft, ...],
        *,
        expected_sequence: int,
    ) -> tuple[Thread, bool]:
        """接收私有冻结批次；调用方负责 BEGIN、COMMIT 和回滚。"""
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
            return thread, False
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
        return thread, True

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
            thread = await self._validated_replay(database, thread_id, None, frozenset())
            await self._save(database, thread)
            await database.commit()
            return thread

    async def _validated_replay(
        self,
        database: aiosqlite.Connection,
        thread_id: UUID,
        through_sequence: int | None,
        ancestors: frozenset[UUID],
    ) -> Thread:
        if thread_id in ancestors:
            raise KernelError("thread_fork_invalid", "Fork来源链存在循环")
        events = await self._events(database, thread_id, 0)
        if through_sequence is not None:
            if len(events) < through_sequence:
                raise KernelError("thread_fork_invalid", "Fork来源事件前缀缺失")
            events = events[:through_sequence]
        thread = replay(events)
        snapshot = thread.fork_snapshot
        if snapshot is not None:
            source = await self._validated_replay(
                database,
                snapshot.source_thread_id,
                snapshot.source_sequence,
                ancestors | {thread_id},
            )
            validate_fork_snapshot(source, snapshot)
        return thread
