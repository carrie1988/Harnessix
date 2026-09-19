from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "archive_legacy_action_state.py"


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as database:
        database.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE actions (
                action_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL
            );
            CREATE TABLE action_events (
                action_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (action_id, sequence)
            );
            INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00Z');
            INSERT INTO schema_migrations VALUES (2, '2026-01-02T00:00:00Z');
            INSERT INTO actions VALUES ('a-1', 'succeeded', '{"secret":"must-not-leak"}');
            INSERT INTO actions VALUES ('a-2', 'unknown', '{"command":"private"}');
            INSERT INTO action_events VALUES ('a-1', 1, '{"body":"private"}');
            INSERT INTO action_events VALUES ('a-2', 1, '{"body":"private"}');
            INSERT INTO action_events VALUES ('a-2', 2, '{"body":"private"}');
            """
        )


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def test_inspect_and_archive_preserve_snapshot_without_payloads(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    archive = tmp_path / "legacy.archive.db"
    manifest = tmp_path / "legacy.manifest.json"
    _legacy_database(source)

    inspected = _run("inspect", "--source", str(source))
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout) == {
        "action_count": 2,
        "event_count": 3,
        "schema_versions": [1, 2],
        "status_counts": {"succeeded": 1, "unknown": 1},
    }

    result = _run(
        "archive",
        "--source",
        str(source),
        "--output",
        str(archive),
        "--manifest",
        str(manifest),
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert json.loads(manifest.read_text(encoding="utf-8")) == record
    assert record["source_name"] == source.name
    assert record["archive_name"] == archive.name
    assert record["action_count"] == 2
    assert record["event_count"] == 3
    assert record["schema_versions"] == [1, 2]
    serialized = manifest.read_text(encoding="utf-8")
    assert "must-not-leak" not in serialized
    assert "private" not in serialized
    assert str(tmp_path) not in serialized
    with sqlite3.connect(f"{archive.resolve().as_uri()}?mode=ro", uri=True) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("SELECT COUNT(*) FROM actions").fetchone() == (2,)
        assert database.execute("SELECT COUNT(*) FROM action_events").fetchone() == (3,)
    if os.name == "posix":
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_archive_rejects_wrong_schema_symlink_and_existing_output(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.db"
    with sqlite3.connect(wrong) as database:
        database.execute("CREATE TABLE unrelated (value TEXT)")
    wrong_result = _run("inspect", "--source", str(wrong))
    assert wrong_result.returncode == 2
    assert "不是受支持" in wrong_result.stderr

    source = tmp_path / "legacy.db"
    _legacy_database(source)
    output = tmp_path / "archive.db"
    output.write_bytes(b"existing")
    existing = _run("archive", "--source", str(source), "--output", str(output))
    assert existing.returncode == 2
    assert output.read_bytes() == b"existing"

    if hasattr(os, "symlink"):
        link = tmp_path / "legacy-link.db"
        try:
            link.symlink_to(source)
        except OSError:
            pytest.skip("当前环境不允许创建符号链接")
        linked = _run("inspect", "--source", str(link))
        assert linked.returncode == 2
        assert "符号链接" in linked.stderr


def test_archive_captures_committed_wal_without_copying_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    archive = tmp_path / "legacy.archive.db"
    _legacy_database(source)

    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO actions VALUES (?, ?, ?)",
            ("a-wal", "ready", '{"body":"wal-private"}'),
        )
        writer.execute(
            "INSERT INTO action_events VALUES (?, ?, ?)",
            ("a-wal", 1, '{"body":"wal-private"}'),
        )
        writer.commit()
        assert source.with_name(source.name + "-wal").stat().st_size > 0

        result = _run("archive", "--source", str(source), "--output", str(archive))

    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["action_count"] == 3
    assert record["event_count"] == 4
    with sqlite3.connect(f"{archive.resolve().as_uri()}?mode=ro", uri=True) as database:
        assert database.execute(
            "SELECT status FROM actions WHERE action_id = 'a-wal'"
        ).fetchone() == ("ready",)
