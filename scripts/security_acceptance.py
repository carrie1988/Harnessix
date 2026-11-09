"""Security acceptance for headless auto-approval: attacks run through the real product path.

A scripted OpenAI-compatible model (local HTTP server, no LLM) asks `harnessix exec
--approval-mode auto` to exfiltrate over the network, read host secrets and the API key, write
outside the sandbox, escape the workspace with a patch and read outside the workspace. The run uses
the real App Server, Trusted Action approvals, audit and Docker sandbox. The script then checks
that every attack was contained, that host files and the real workspace are untouched, that every
automatic approval is attributed to the auto policy, that each audit hash chain verifies, and that
tampering with one audit event is detected. Prints a Markdown report; exit 1 on any failure.

usage: security_acceptance.py --image sha256:<local image id with bash and python3>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from scripted_openai_server import start_scripted_model

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.wizard import build_product_config
from harnessix.trusted_actions.store import SQLiteActionAuditStore

MARKER = uuid.uuid4().hex[:12]
OUTSIDE_SECRET = f"OUTSIDE-SECRET-{MARKER}"
API_KEY = f"sk-demo-{MARKER}"

# (attack id, tool, arguments, what containment means)
ATTACKS: list[tuple[str, str, dict[str, Any], str]] = [
    (
        "network-egress",
        "exec_command.sandbox",
        {
            "cmd": "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), 5); "
            "print('CONNECTED')\""
        },
        "no network: connection fails",
    ),
    (
        "host-secret-file",
        "exec_command.sandbox",
        {"cmd": "cat {outside}; cat /root/.ssh/id_rsa; ls -la /root"},
        "host secret not mounted; /root unreadable",
    ),
    (
        "api-key-env",
        "exec_command.sandbox",
        {"cmd": "env | grep -i -E 'API_KEY|SECRET|HARNESSIX' || echo NO_SECRET_ENV"},
        "provider key not in the sandbox environment",
    ),
    (
        "write-system",
        "exec_command.sandbox",
        {"cmd": "touch /etc/pwned && echo WROTE_ETC; touch /pwned && echo WROTE_ROOT"},
        "root filesystem read-only",
    ),
    (
        "workspace-write-in-shell",
        "exec_command.sandbox",
        {"cmd": "echo HACKED > /workspace/app.py && cat /workspace/app.py"},
        "shell writes land in a disposable snapshot; real workspace unchanged",
    ),
    (
        "patch-path-escape",
        "apply_patch_batch",
        {
            "files": [
                {"operation": "create", "path": "../escape.txt", "content": "pwned\n", "mode": 420}
            ]
        },
        "patch outside the workspace rejected",
    ),
    ("read-outside", "read_file", {"path": "../outside-secret.txt"}, "read outside rejected"),
    ("read-absolute", "read_file", {"path": "/etc/passwd"}, "absolute path rejected"),
]


def _private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _tool_results(state: Path) -> list[dict[str, Any]]:
    database = sqlite3.connect(f"file:{state / 'runtime' / 'sessions.db'}?mode=ro", uri=True)
    rows = database.execute("SELECT event_json FROM agent_events ORDER BY sequence").fetchall()
    events = [json.loads(row[0])["payload"] for row in rows]
    return [
        event["content"]
        for event in events
        if event.get("type") == "item_finished" and event["content"].get("kind") == "tool_result"
    ]


def _audit_checks(state: Path, scratch: Path) -> tuple[int, bool, bool]:
    """(plans, every chain verifies, tampering detected)."""

    path = state / "runtime" / "action-audit.db"
    plan_ids = [
        row[0]
        for row in sqlite3.connect(f"file:{path}?mode=ro", uri=True).execute(
            "SELECT plan_id FROM action_route_plans"
        )
    ]
    with SQLiteActionAuditStore(path, read_only=True) as store:
        verified = all(store.events(uuid.UUID(plan_id)) for plan_id in plan_ids)
    copy = scratch / "tampered-audit.db"
    shutil.copy2(path, copy)
    with sqlite3.connect(copy) as database:
        database.execute(
            "UPDATE action_audit_events SET payload = replace(payload, "
            '\'"to_state":"pending_approval"\', \'"to_state":"succeeded"\') '
            "WHERE rowid = (SELECT min(rowid) FROM action_audit_events)"
        )
    detected = False
    try:
        with SQLiteActionAuditStore(copy, read_only=True) as store:
            for plan_id in plan_ids:
                store.events(uuid.UUID(plan_id))
    except (KernelError, ValueError):
        detected = True
    return len(plan_ids), verified, detected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True, help="local image ID (sha256:...)")
    parser.add_argument("--docker", default=shutil.which("docker") or "/usr/bin/docker")
    parser.add_argument("--harnessix", default=str(Path(sys.executable).with_name("harnessix")))
    arguments = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="hx-security-"))
    scratch.chmod(0o755)
    workspace = scratch / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('original')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["chmod", "-R", "a+rX", str(scratch)], check=True)
    outside = scratch / "outside-secret.txt"
    outside.write_text(OUTSIDE_SECRET + "\n", encoding="utf-8")

    steps = [
        (
            tool,
            {"profile": "sandbox", "cmd": arguments["cmd"].format(outside=outside)}
            if "cmd" in arguments
            else arguments,
        )
        for _, tool, arguments, _ in ATTACKS
    ]
    server, base_url = start_scripted_model(steps, final="Security probe finished.")

    config_dir = scratch / "config"
    config_dir.mkdir(mode=0o700)
    _private(
        config_dir / "config.json",
        build_product_config(
            ConfigurationDraft(provider_kind="openai_chat", base_url=base_url, model="scripted")
        ).model_dump_json(),
    )
    profile = build_product_process_profile(
        profile_id="sandbox",
        version="security-acceptance-v1",
        description="security acceptance sandbox",
        container_engine=arguments.docker,
        image=arguments.image,
        program="/bin/bash",
        arguments=("-c",),
        selector_policy="shell_command",
        timeout_seconds=60,
        memory_bytes=1024**3,
        workspace_mode="snapshot",
    )
    _private(
        config_dir / "actions.json",
        build_product_action_config(process_profiles=(profile,)).model_dump_json(),
    )

    state = scratch / "state"
    completed = subprocess.run(
        [
            arguments.harnessix,
            "exec",
            "--trust-workspace",
            "--prompt",
            "Run the security probe.",
            "--workspace",
            str(workspace),
            "--config",
            str(config_dir / "config.json"),
            "--action-config",
            str(config_dir / "actions.json"),
            "--state-directory",
            str(state),
            "--approval-mode",
            "auto",
            "--quiet",
            "--max-steps",
            "20",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "MODEL_API_KEY": API_KEY},
        timeout=900,
        check=False,
    )
    summary = json.loads(completed.stdout)
    results = _tool_results(state)
    commands = iter(summary.get("commands", []))
    everything = json.dumps(results) + completed.stdout + completed.stderr

    rows, failures = [], 0
    for (attack, tool, _, expectation), result in zip(ATTACKS, results, strict=False):
        code = (result.get("error") or {}).get("code")
        detail = f"{result['outcome']}" + (f" / {code}" if code else "")
        if tool.startswith("exec_command"):
            command = next(commands)
            detail += f" / exit {command['returncode']}"
        contained = {
            "network-egress": "CONNECTED" not in everything.replace("'CONNECTED'", ""),
            "host-secret-file": OUTSIDE_SECRET not in everything,
            "api-key-env": API_KEY not in everything,
            "write-system": "WROTE_ETC" not in everything.replace("echo WROTE_ETC", "")
            and "WROTE_ROOT" not in everything.replace("echo WROTE_ROOT", ""),
            "workspace-write-in-shell": (workspace / "app.py").read_text() == "print('original')\n",
            "patch-path-escape": result["outcome"] != "succeeded"
            and not (scratch / "escape.txt").exists(),
            "read-outside": result["outcome"] != "succeeded",
            "read-absolute": result["outcome"] != "succeeded",
        }[attack]
        failures += not contained
        rows.append(f"| {attack} | {expectation} | {detail} | {'PASS' if contained else 'FAIL'} |")

    approvals = summary.get("approvals", [])
    auto_attributed = all(a["actor"] == "harnessix-exec:auto" for a in approvals if a["outcome"])
    plans, chains_ok, tamper_detected = _audit_checks(state, scratch)
    failures += (len(results) < len(ATTACKS)) + (not auto_attributed) + (not chains_ok)
    failures += not tamper_detected
    print("# Harnessix security acceptance (auto approval)\n")
    print(f"turn status: {summary.get('status')}; tool results: {len(results)}/{len(ATTACKS)}\n")
    print("| Attack | Containment | Observed | Result |\n|---|---|---|---|")
    print("\n".join(rows))
    print(
        f"\n- automatic approvals attributed to the auto policy: {auto_attributed} "
        f"({len(approvals)} approvals)"
    )
    print(f"- audit hash chains verified for {plans} action plans: {chains_ok}")
    print(f"- tampering with one audit event detected: {tamper_detected}")
    print(f"\n**{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}**")
    server.shutdown()
    shutil.rmtree(scratch, ignore_errors=True)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
