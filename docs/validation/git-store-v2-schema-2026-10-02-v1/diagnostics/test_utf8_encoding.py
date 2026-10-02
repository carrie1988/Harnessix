"""真实SQLite编码边界：CAST按数据库编码，v2必须固定UTF-8而不是放宽字节上限。"""

import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store_schema_v2 import (
    GIT_STORE_V2_DDL,
    _create_git_store_v2_tables,
    verify_git_store_v2_schema,
)
from tests.delivery.test_git_store_readonly import _ORIGINAL_V1_DDL


@pytest.mark.parametrize("reader", [False, True])
@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
def test_non_utf8_structure_cannot_weaken_payload_byte_bound(encoding: str, reader: bool) -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute(f"PRAGMA encoding='{encoding}'")
        db.executescript(_ORIGINAL_V1_DDL)
        db.execute("INSERT INTO git_delivery_metadata VALUES ('schema_version','1')")
        text = "汉" * (64 * 1024 * 1024 // 3 + 1)
        assert len(text.encode("utf-8")) > 64 * 1024 * 1024
        assert (
            db.execute("SELECT length(CAST(? AS BLOB))", (text,)).fetchone()[0] < 64 * 1024 * 1024
        )
        if reader:
            db.executescript(GIT_STORE_V2_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
            db.execute("UPDATE git_delivery_metadata SET value='2'")
        db.execute("BEGIN")
        with pytest.raises(KernelError) as caught:
            (verify_git_store_v2_schema if reader else _create_git_store_v2_tables)(db)
        assert caught.value.code == "git_delivery_store_corrupt"
        db.execute("ROLLBACK")
