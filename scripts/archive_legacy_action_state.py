"""只读检查并归档已退役的独立 Action Plane SQLite 状态库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REQUIRED_TABLES: Final = frozenset({"actions", "action_events", "schema_migrations"})


class ArchiveError(RuntimeError):
    """归档输入、旧库结构或原子写入不满足安全约束。"""


@dataclass(frozen=True, slots=True)
class LegacyStateSummary:
    schema_versions: tuple[int, ...]
    action_count: int
    event_count: int
    status_counts: dict[str, int]


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _regular_source(path: Path) -> Path:
    if path.is_symlink():
        raise ArchiveError("源数据库不能是符号链接")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise ArchiveError("源数据库不存在") from None
    if not resolved.is_file():
        raise ArchiveError("源数据库必须是普通文件")
    return resolved


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _summary(connection: sqlite3.Connection) -> LegacyStateSummary:
    integrity = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
    if integrity != ("ok",):
        raise ArchiveError("源数据库完整性检查失败")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    }
    missing = sorted(REQUIRED_TABLES - tables)
    if missing:
        raise ArchiveError(f"源数据库不是受支持的旧 Action Plane 状态库：缺少 {', '.join(missing)}")
    versions = tuple(
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    )
    action_count = int(connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0])
    event_count = int(connection.execute("SELECT COUNT(*) FROM action_events").fetchone()[0])
    status_counts = {
        str(status): int(count)
        for status, count in connection.execute(
            "SELECT status, COUNT(*) FROM actions GROUP BY status ORDER BY status"
        )
    }
    return LegacyStateSummary(
        schema_versions=versions,
        action_count=action_count,
        event_count=event_count,
        status_counts=status_counts,
    )


def inspect(path: Path) -> LegacyStateSummary:
    """在 SQLite 只读模式下返回低敏感度统计，不读取请求或事件正文。"""

    source = _regular_source(path)
    with closing(_readonly_connection(source)) as connection:
        return _summary(connection)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exclusive_target(path: Path, label: str) -> Path:
    if path.is_symlink() or path.exists():
        raise ArchiveError(f"{label}已存在，归档不会覆盖任何文件")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ArchiveError(f"{label}父目录无效")
    return parent / path.name


def _link_without_overwrite(temporary: Path, target: Path, label: str) -> None:
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise ArchiveError(f"{label}已存在，归档不会覆盖任何文件") from None
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directories(*directories: Path) -> None:
    """POSIX上持久化目录项；Windows不提供可移植的目录fsync。"""

    if os.name != "posix":
        return
    for directory in dict.fromkeys(directories):
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def archive(source_path: Path, output_path: Path, manifest_path: Path) -> dict[str, object]:
    """通过 SQLite Online Backup 获取一致快照，并以不覆盖语义发布归档。"""

    source = _regular_source(source_path)
    output = _exclusive_target(output_path, "归档文件")
    manifest = _exclusive_target(manifest_path, "清单文件")
    if output == manifest or source in {output, manifest}:
        raise ArchiveError("源、归档和清单路径必须互不相同")

    output_fd, output_temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    output_temp = Path(output_temp_name)
    os.close(output_fd)
    manifest_temp: Path | None = None
    published_output = False
    try:
        os.chmod(output_temp, 0o600)
        with closing(_readonly_connection(source)) as source_db:
            source_summary = _summary(source_db)
            with closing(sqlite3.connect(output_temp)) as archive_db:
                source_db.backup(archive_db)
        with closing(_readonly_connection(output_temp)) as archive_db:
            archived_summary = _summary(archive_db)
        if archived_summary != source_summary:
            raise ArchiveError("归档快照统计与源数据库不一致")
        with output_temp.open("rb") as stream:
            os.fsync(stream.fileno())

        record: dict[str, object] = {
            "schema_version": "harnessix.legacy-action-archive/v1",
            "created_at": datetime.now(UTC).isoformat(),
            "source_name": source.name,
            "archive_name": output.name,
            "archive_sha256": _sha256(output_temp),
            "archive_size_bytes": output_temp.stat().st_size,
            **asdict(archived_summary),
        }
        manifest_fd, manifest_temp_name = tempfile.mkstemp(
            prefix=f".{manifest.name}.", dir=manifest.parent
        )
        manifest_temp = Path(manifest_temp_name)
        os.chmod(manifest_temp, 0o600)
        with os.fdopen(manifest_fd, "wb") as stream:
            stream.write(_json_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())

        _link_without_overwrite(output_temp, output, "归档文件")
        published_output = True
        _link_without_overwrite(manifest_temp, manifest, "清单文件")
        manifest_temp = None
        _fsync_directories(output.parent, manifest.parent)
        return record
    except BaseException:
        output_temp.unlink(missing_ok=True)
        if manifest_temp is not None:
            manifest_temp.unlink(missing_ok=True)
        if published_output:
            output.unlink(missing_ok=True)
            _fsync_directories(output.parent)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读检查或归档已退役的独立 Action Plane SQLite 状态库"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="只输出表结构与低敏感度统计")
    inspect_parser.add_argument("--source", type=Path, required=True)
    archive_parser = commands.add_parser("archive", help="生成一致的只读归档和校验清单")
    archive_parser.add_argument("--source", type=Path, required=True)
    archive_parser.add_argument("--output", type=Path, required=True)
    archive_parser.add_argument("--manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            value: object = asdict(inspect(args.source))
        else:
            manifest = args.manifest or args.output.with_suffix(
                args.output.suffix + ".manifest.json"
            )
            value = archive(args.source, args.output, manifest)
    except (ArchiveError, OSError, sqlite3.Error) as error:
        sys.stderr.buffer.write(f"legacy_action_archive_error: {error}\n".encode())
        return 2
    sys.stdout.buffer.write(_json_bytes(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
