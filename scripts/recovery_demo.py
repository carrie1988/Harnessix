"""Crash-recovery demo: kill -9 a headless run mid-command, then resume it from durable state.

A scripted model asks `harnessix exec --approval-mode auto` to run one sandboxed command that
appends "start" to the task-persistent dependency root, sleeps, then appends "end". While the
command is sleeping, the whole exec process group (client, App Server, process owner) is killed
with SIGKILL. `harnessix exec --resume-thread` then reopens the same state directory. The demo
reports what recovery concluded about the in-flight command and checks that it was never replayed
(exactly one "start" line). Prints a Markdown report; exit 1 if the command ran twice.

usage: recovery_demo.py --image sha256:<local image id with bash>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from scripted_openai_server import start_scripted_model

from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.wizard import build_product_config

COMMAND = "echo start >> /harnessix/deps/runs.log; sleep 25; echo end >> /harnessix/deps/runs.log"


def _private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _wait_for(predicate: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def _runs_log(state: Path) -> Path | None:
    found = list(state.glob("runtime/task-dependencies/*/*/deps/runs.log"))
    return found[0] if found else None


def _thread_id(state: Path) -> str:
    database = sqlite3.connect(f"file:{state / 'runtime' / 'sessions.db'}?mode=ro", uri=True)
    return str(
        database.execute("SELECT thread_id FROM agent_events ORDER BY sequence LIMIT 1").fetchone()[
            0
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--docker", default=shutil.which("docker") or "/usr/bin/docker")
    parser.add_argument("--harnessix", default=str(Path(sys.executable).with_name("harnessix")))
    arguments = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="hx-recovery-"))
    scratch.chmod(0o755)
    workspace = scratch / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["chmod", "-R", "a+rX", str(scratch)], check=True)

    server, base_url = start_scripted_model(
        [("exec_command.sandbox", {"profile": "sandbox", "cmd": COMMAND})],
        final="Command finished.",
    )
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
        version="recovery-demo-v1",
        description="recovery demo sandbox",
        container_engine=arguments.docker,
        image=arguments.image,
        program="/bin/bash",
        arguments=("-c",),
        selector_policy="shell_command",
        timeout_seconds=120,
        memory_bytes=1024**3,
        persistent_dependencies=True,
    )
    _private(
        config_dir / "actions.json",
        build_product_action_config(process_profiles=(profile,)).model_dump_json(),
    )
    state = scratch / "state"
    common = [
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
    ]
    # 本地端点不校验密钥，但产品仍要求设置；值会作为Secret参与公开输出泄露检测，须足够独特。
    environment = {**os.environ, "MODEL_API_KEY": f"sk-local-{uuid.uuid4().hex}"}

    first = subprocess.Popen(
        [arguments.harnessix, "exec", "--prompt", "Run the long command.", *common],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    started = _wait_for(
        lambda: (log := _runs_log(state)) is not None and "start" in log.read_text(), 120
    )
    os.killpg(first.pid, signal.SIGKILL)  # 客户端、App Server与Process Owner同时消失
    first.wait()
    if not started:
        print("command did not start:", first.stderr.read().decode(errors="replace")[-2000:])
        print(sorted(str(path.relative_to(state)) for path in state.rglob("*"))[:40])
    killed_at = time.monotonic()
    thread_id = _thread_id(state)

    resumed = subprocess.run(
        [arguments.harnessix, "exec", "--resume-thread", thread_id, *common],
        capture_output=True,
        text=True,
        env=environment,
        timeout=600,
        check=False,
    )
    summary = json.loads(resumed.stdout) if resumed.stdout.strip() else {}
    time.sleep(max(0.0, 30 - (time.monotonic() - killed_at)))  # 等原命令自然结束
    log = _runs_log(state)
    lines = log.read_text().split() if log is not None else []
    commands = summary.get("commands", [])

    print("# Harnessix crash-recovery demo\n")
    print(f"- command started inside the sandbox before the kill: {started}")
    print("- killed the exec process group with SIGKILL while the command slept")
    print(
        f"- resumed thread `{thread_id}`: turn status **{summary.get('status')}**, "
        f"error `{(summary.get('error') or {}).get('code')}`"
    )
    for command in commands:
        print(
            f"- recovered command outcome: `{command['outcome']}`, exit `{command['returncode']}`"
        )
    print(f"- runs.log after recovery: {lines} (one `start` means the command was not replayed)")
    ok = started and lines.count("start") == 1
    print(f"\n**{'PASS' if ok else 'FAIL'}**")
    server.shutdown()
    shutil.rmtree(scratch, ignore_errors=True)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
