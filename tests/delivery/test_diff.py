from pathlib import Path

from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore


def test_diff_covers_text_binary_mode_and_exact_rename(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old-name.txt").write_bytes(b"same\n")
    (workspace / "modify.txt").write_bytes(b"before\n")
    prepared = prepare_workspace_transaction(
        workspace,
        {
            "old-name.txt": DesiredWorkspaceFile(None),
            "new-name.txt": DesiredWorkspaceFile(b"same\n", 0o644),
            "modify.txt": DesiredWorkspaceFile(b"after\n", 0o755),
            "binary.dat": DesiredWorkspaceFile(b"\0\x01", 0o644),
        },
        request_id="diff",
    )
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(prepared)
        document = build_workspace_diff(record.plan, store)
    entries = {entry.kind: entry for entry in document.entries if entry.kind != "added"}
    assert entries["renamed"].original_path == "old-name.txt"
    assert entries["renamed"].path == "new-name.txt"
    assert entries["modified"].path == "modify.txt"
    assert "-before" in document.text and "+after" in document.text
    assert "old mode 644\nnew mode 755" in document.text
    binary = next(entry for entry in document.entries if entry.path == "binary.dat")
    assert binary.kind == "added" and binary.binary
    assert "Binary files differ" in document.text
    assert document.utf8_bytes == len(document.text.encode())
