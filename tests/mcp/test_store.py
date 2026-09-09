from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.mcp.contracts import (
    McpCatalogSnapshot,
    McpServerIdentity,
    McpToolSnapshot,
    mcp_catalog_snapshot_digest,
    mcp_tool_snapshot_digest,
)
from harnessix.mcp.store import SQLiteMcpStore


def catalog(server_id: str, generation: int) -> McpCatalogSnapshot:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    tool_candidate = McpToolSnapshot.model_construct(
        _fields_set=None,
        raw_name="search",
        model_name=f"mcp__{server_id}__search",
        title=None,
        description="search",
        input_schema=schema,
        output_schema=None,
        annotations={},
        definition_sha256=canonical_digest({"raw": "search"}),
        tool_sha256="0" * 64,
    )
    tool = McpToolSnapshot(
        **tool_candidate.model_dump(exclude={"tool_sha256"}),
        tool_sha256=mcp_tool_snapshot_digest(tool_candidate),
    )
    identity = McpServerIdentity(
        server_id=server_id,
        transport="in_process",
        target_sha256=canonical_digest("target"),
        protocol_version="2026-07-28",
        reported_name="test",
        reported_version="1",
        capabilities_sha256=canonical_digest({"tools": {}}),
    )
    candidate = McpCatalogSnapshot.model_construct(
        _fields_set=None,
        server=identity,
        generation=generation,
        captured_at=utc_now(),
        tools=(tool,),
        catalog_sha256="0" * 64,
    )
    return McpCatalogSnapshot(
        **candidate.model_dump(exclude={"catalog_sha256"}),
        catalog_sha256=mcp_catalog_snapshot_digest(candidate),
    )


def test_store_persists_catalog_and_hash_chained_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "state/mcp.db"
    store = SQLiteMcpStore(path)

    assert store.begin_connect("books").state == "connecting"
    first = catalog("books", 1)
    connected = store.connected(first)
    assert connected.state == "connected"
    assert store.catalog("books") == first
    assert [event.to_state for event in store.events("books")] == [
        "connecting",
        "connected",
    ]

    store.closed("books")
    store.begin_connect("books")
    second = catalog("books", 2)
    store.connected(second)
    assert second.catalog_sha256 == first.catalog_sha256
    assert store.catalog("books") == second
    assert store.load("books").generation == 2
    store.close()

    reopened = SQLiteMcpStore(path)
    assert reopened.load("books").state == "connected"
    assert reopened.recover_interrupted() == ("books",)
    assert reopened.load("books").error_code == "mcp_host_interrupted"
    assert reopened.recover_interrupted() == ()


def test_store_rejects_projection_or_event_corruption(tmp_path: Path) -> None:
    path = tmp_path / "mcp.db"
    store = SQLiteMcpStore(path)
    store.begin_connect("books")
    store.connected(catalog("books", 1))
    store.close()

    db = sqlite3.connect(path)
    db.execute(
        "UPDATE mcp_connection_events SET digest = ? WHERE server_id = ? AND sequence = 2",
        ("f" * 64, "books"),
    )
    db.commit()
    db.close()

    corrupt = SQLiteMcpStore(path)
    with pytest.raises(KernelError) as caught:
        corrupt.load("books")
    assert caught.value.code == "mcp_store_corrupt"


def test_store_rejects_parallel_connect_without_recovery(tmp_path: Path) -> None:
    store = SQLiteMcpStore(tmp_path / "mcp.db")
    store.begin_connect("books")
    store.connected(catalog("books", 1))

    with pytest.raises(KernelError) as caught:
        store.begin_connect("books")
    assert caught.value.code == "mcp_connection_state_conflict"
