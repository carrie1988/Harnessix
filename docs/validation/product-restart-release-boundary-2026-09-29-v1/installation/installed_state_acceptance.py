"""源码之外验证实际安装产品的互斥、全状态恢复与稳定终态，不发送模型Turn。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import harnessix
from harnessix.product_config.state_restore_contracts import ProductStateRestoreResult
from harnessix.sdk.agent_client import AgentClient
from harnessix.sdk.subprocess import SubprocessAgentTransport


def cli(command: tuple[str, ...], *, success: bool = True) -> dict:
    result = subprocess.run(command, capture_output=True, timeout=60)
    assert result.returncode == (0 if success else 2), (result.returncode, result.stderr)
    return json.loads(result.stdout if success else result.stderr)


async def accept(root: Path) -> dict:
    workspace = root / "work/workspace"
    workspace.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    (workspace / "preserved.txt").write_bytes(b"unchanged workspace\n")
    state = root / "state"
    backup = root / "backup"
    config = root / "config.json"
    os.environ["HARNESSIX_INSTALL_FIXTURE_KEY"] = "install-fixture-not-a-real-key"
    os.environ.pop("PYTHONPATH", None)
    executable = root / "venv/bin/harnessix"
    assert str(root / "venv") in str(harnessix.__file__)
    assert not any("Downloads/sources/Harnessix" in path for path in sys.path)
    assert sys.flags.isolated
    doctor = await asyncio.to_thread(
        cli,
        (
            str(executable),
            "code",
            "doctor",
            str(workspace),
            "--config",
            str(config),
            "--state-directory",
            str(state),
            "--json",
        ),
    )
    assert doctor["ready"] is True
    (root / "doctor.json").write_text(
        json.dumps(doctor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    command = (
        sys.executable,
        "-I",
        "-m",
        "harnessix",
        "agent-server",
        "--config",
        str(config),
        "--workspace",
        str(workspace),
        "--state-directory",
        str(state),
    )

    async def session(create: str | None = None):
        transport = SubprocessAgentTransport(command)
        client = AgentClient(transport)
        try:
            async with asyncio.timeout(30):
                await client.initialize()
                thread = (
                    None
                    if create is None
                    else await client.create_thread(str(workspace), request_id=create)
                )
                page = await client.list_threads(limit=50)
            assert page.next_cursor is None
            return thread, {item.thread_id for item in page.threads}
        finally:
            await client.close()
            assert transport.snapshot().state == "closed"

    transport = SubprocessAgentTransport(command)
    client = AgentClient(transport)
    try:
        async with asyncio.timeout(30):
            await client.initialize()
            first = await client.create_thread(str(workspace), request_id="installed-before-backup")
        busy = await asyncio.to_thread(
            cli,
            (
                str(executable),
                "state",
                "backup",
                "--state-directory",
                str(state),
                "--backup-directory",
                str(backup),
            ),
            success=False,
        )
        assert busy["code"] == "product_state_busy"
        assert not backup.exists()
    finally:
        await client.close()
    assert transport.snapshot().state == "closed"
    key = state / "session-auth/key.v1"
    key_digest = hashlib.sha256(key.read_bytes()).digest()
    created = await asyncio.to_thread(
        cli,
        (
            str(executable),
            "state",
            "backup",
            "--state-directory",
            str(state),
            "--backup-directory",
            str(backup),
        ),
    )
    assert created["status"] == "backed_up"
    verified = await asyncio.to_thread(
        cli,
        (
            str(executable),
            "state",
            "verify",
            "--state-directory",
            str(state),
            "--backup-directory",
            str(backup),
        ),
    )
    assert verified["status"] == "verified" and verified["backup_id"] == created["backup_id"]
    second, observed = await session("installed-after-backup")
    assert second is not None and observed == {first.thread_id, second.thread_id}
    restore_command = (
        str(executable),
        "state",
        "restore",
        "--state-directory",
        str(state),
        "--backup-directory",
        str(backup),
        "--restore-id",
        str(uuid4()),
        "--confirm-backup",
        created["backup_id"],
    )
    restored = ProductStateRestoreResult.model_validate(
        await asyncio.to_thread(cli, restore_command)
    )
    assert restored.status == "restored" and restored.retained_previous_state
    third, observed = await session("installed-after-restore")
    assert third is not None and observed == {first.thread_id, third.thread_id}
    repeated = ProductStateRestoreResult.model_validate(
        await asyncio.to_thread(cli, restore_command)
    )
    assert repeated == restored
    _, final_observed = await session()
    assert final_observed == observed
    assert hashlib.sha256(key.read_bytes()).digest() == key_digest
    assert (workspace / "preserved.txt").read_bytes() == b"unchanged workspace\n"
    return {
        "spec_version": "harnessix.installed-state-acceptance/v1",
        "source_revision": "1bc3794bfbdb9ce5fa58103d372c68de4401f90a",
        "platform": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "import_location": harnessix.__file__,
        "isolated": True,
        "source_checkout_in_sys_path": False,
        "doctor_ready": True,
        "active_owner_backup_refused": True,
        "full_backup_verified": True,
        "backup_file_count": created["files"],
        "original_key_retained": True,
        "old_thread_restored": True,
        "post_snapshot_thread_removed": True,
        "prior_root_retained": True,
        "same_restore_id_has_same_terminal_result": True,
        "same_restore_id_does_not_rewind_new_state": True,
        "workspace_unchanged": True,
        "provider_turn_requests": 0,
        "commercial_release": False,
        "not_proven": [
            "real_coding_task",
            "version_upgrade",
            "uninstall",
            "linux_install",
            "windows_install",
            "beta",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-root", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(accept(args.environment_root.resolve()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
