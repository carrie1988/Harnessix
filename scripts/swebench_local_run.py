"""Run a frozen SWE-bench task list through the product path (`harnessix exec`) on local replicas.

Per task the runner
1. derives a leak-free workspace from the official instance image: `/testbed` is copied out, the
   original Git history is dropped and the originally tracked files become one base commit;
2. writes a product config bound to one local model replica and, for shell arms, an action config
   whose command profile runs inside the same instance image (by image ID, no network) with the
   workspace mounted read-only at `/testbed`;
3. runs one headless turn that auto-approves only workspace patches and sandboxed commands;
4. exports the workspace diff as an official prediction.

Results append to `<run>/results.jsonl` and `<run>/predictions.jsonl`; a rerun skips finished
instances, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harnessix.evals.swebench.predictions import collect_workspace_patch
from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.wizard import build_product_config, thinking_preset

MODEL = "qwen3.8-flash-next"
API_KEY_VARIABLE = "MODEL_API_KEY"
CONTAINER_WORKSPACE = "/testbed"
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "harnessix-eval",
    "GIT_AUTHOR_EMAIL": "eval@harnessix.invalid",
    "GIT_COMMITTER_NAME": "harnessix-eval",
    "GIT_COMMITTER_EMAIL": "eval@harnessix.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


@dataclass(frozen=True)
class Arm:
    """One preregistered configuration; arms differ only in budget and command access."""

    name: str
    shell: bool
    max_steps: int
    max_tokens: int
    timeout_seconds: int
    # Thinking stays off for every local arm: K100 decode is ~33 tok/s and stalls under prefill.
    thinking: str = "off"


ARMS = {
    # Product defaults before week 1: 16 steps, 100K tokens, 120 s, no command execution.
    "A0": Arm("A0", shell=False, max_steps=16, max_tokens=100_000, timeout_seconds=120),
    # New product turn budget, still without command execution.
    "A1": Arm("A1", shell=False, max_steps=100, max_tokens=1_000_000, timeout_seconds=3_600),
    # New budget plus the generic container command in the instance image.
    "A2": Arm("A2", shell=True, max_steps=100, max_tokens=1_000_000, timeout_seconds=3_600),
}

SHELL_ENVIRONMENT_NOTE = """
Command environment: every exec_command call starts a fresh container from this task's own
image, network disabled, with the repository mounted read-only at /testbed (the working
directory). Edit files only with the patch tools; the container sees your edits immediately.
For Python start commands with:
  export HOME=/tmp && source /opt/miniconda3/bin/activate testbed && cd /testbed &&
Only /tmp is writable (cleared after each call, not executable); use it for scratch files.
"""


@dataclass(frozen=True)
class Task:
    instance_id: str
    image: str
    repo: str
    base_commit: str
    problem_statement: str


class RunError(Exception):
    """A task could not be prepared or collected; recorded instead of crashing the run."""


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 600,
    stdin: bytes | None = None,
) -> str:
    completed = subprocess.run(
        command, cwd=cwd, env=env, input=stdin, capture_output=True, timeout=timeout, check=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()[-400:]
        raise RunError(f"{command[0]} {command[1]} failed: {detail}")
    return completed.stdout.decode(errors="replace").strip()


# ---------------------------------------------------------------- workspace


def materialize_workspace(task: Task, directory: Path) -> tuple[str, str]:
    """Copy /testbed out of the image into a fresh single-commit repository.

    Returns (image_id, base_commit). Raises RunError when the image does not match the dataset
    row or the result still exposes more than one commit.
    """

    image_id = _run(["docker", "image", "inspect", "--format", "{{.Id}}", task.image])
    container = _run(["docker", "create", "--label", "owner=harnessix", task.image, "/bin/true"])
    try:
        directory.mkdir(parents=True)
        _run(["docker", "cp", f"{container}:/testbed/.", str(directory)], timeout=1800)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)

    git = ["git", "-c", "safe.directory=*", "-C", str(directory)]
    # Current official images add one "SWE-bench" commit (file-mode changes) on top of base.
    original_head = _run([*git, "rev-parse", "HEAD"])
    if task.base_commit not in (original_head, _run([*git, "rev-parse", "HEAD^"])):
        raise RunError(
            f"image HEAD {original_head} is neither base {task.base_commit} nor its child"
        )
    tracked = subprocess.run([*git, "ls-files", "-z"], capture_output=True, check=True).stdout
    for nested in sorted(directory.rglob(".git"), key=lambda path: len(path.parts), reverse=True):
        if nested.is_dir() and not nested.is_symlink():
            shutil.rmtree(nested)
        else:
            nested.unlink()

    environment = {**os.environ, **_GIT_IDENTITY}
    _run([*git, "init", "-q", "-b", "main"], env=environment)
    _run(
        [*git, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
        env=environment,
        stdin=tracked,
    )
    _run([*git, "commit", "-q", "--no-verify", "-m", "SWE-bench base snapshot"], env=environment)
    if _run([*git, "rev-list", "--all", "--count"]) != "1":
        raise RunError("workspace exposes more than one commit")
    # The sandbox runs as an unprivileged user; it needs read and traverse access only.
    _run(["chmod", "-R", "a+rX", str(directory)])
    return image_id, _run([*git, "rev-parse", "HEAD"])


# ---------------------------------------------------------------- configuration


def write_private_json(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)


def product_config_json(port: int, thinking: str) -> str:
    base_url = f"http://127.0.0.1:{port}/v1"
    preset = thinking_preset(base_url, MODEL)
    assert preset is not None, "loopback endpoints always have a preset"
    draft = ConfigurationDraft(
        provider_kind="openai_chat",
        base_url=base_url,
        model=MODEL,
        environment_variable=API_KEY_VARIABLE,
        thinking_protocol=preset[0],
        omit_tool_choice=preset[1],
        thinking=thinking,
    )
    config: str = build_product_config(draft).model_dump_json(indent=2)
    return config


def action_config_json(image_id: str, docker: str) -> str:
    profile = build_product_process_profile(
        profile_id="testbed",
        version="swebench-instance-image-v1",
        description="在本任务的SWE-bench实例镜像中运行命令（源码只读挂载于/testbed）",
        container_engine=docker,
        image=image_id,
        program="/bin/bash",
        arguments=("-c",),
        selector_policy="shell_command",
        timeout_seconds=900,
        cpu_limit=4.0,
        memory_bytes=8 * 1024**3,
        process_limit=1024,
        tmpfs_bytes=2 * 1024**3,
        workspace_path=CONTAINER_WORKSPACE,
    )
    config: str = build_product_action_config(process_profiles=(profile,)).model_dump_json(indent=2)
    return config


def task_prompt(task: Task, arm: Arm) -> str:
    return (
        "You are solving one SWE-bench task in the current repository.\n"
        f"Hard task limits: {arm.max_tokens} cumulative tokens, {arm.max_steps} model steps, "
        f"{arm.timeout_seconds} seconds.\n"
        "Work autonomously; leave the smallest correct patch to non-test source files. Do not ask "
        "questions or use network access, hidden tests, reference patches or evaluator artifacts.\n"
        + (SHELL_ENVIRONMENT_NOTE if arm.shell else "")
        + f"\nRepository: {task.repo}\n\nProblem statement:\n{task.problem_statement}"
    )


# ---------------------------------------------------------------- one task


@dataclass
class Outcome:
    instance_id: str
    arm: str
    port: int
    status: str
    exit_code: int | None
    elapsed_seconds: float
    model_steps: int | None
    usage: dict[str, Any] | None
    patch_bytes: int
    changed_paths: list[str] | None
    error: str | None


def run_task(
    task: Task, arm: Arm, port: int, run_root: Path, options: argparse.Namespace
) -> tuple[Outcome, str]:
    task_root = run_root / "tasks" / task.instance_id
    if task_root.exists():
        shutil.rmtree(task_root)  # a previous attempt died before recording a result
    task_root.mkdir(parents=True)
    task_root.chmod(0o755)
    workspace = task_root / "workspace"
    started = time.monotonic()
    summary: dict[str, Any] = {}
    exit_code: int | None = None
    patch = ""
    try:
        image_id, base_commit = materialize_workspace(task, workspace)
        write_private_json(
            task_root / "product-config.json", product_config_json(port, arm.thinking)
        )
        command = [
            options.harnessix,
            "exec",
            "--prompt-file",
            str(task_root / "prompt.txt"),
            "--workspace",
            str(workspace),
            "--config",
            str(task_root / "product-config.json"),
            "--state-directory",
            str(task_root / "state"),
            "--approval-mode",
            "auto",
            "--summary-file",
            str(task_root / "summary.json"),
            "--quiet",
            "--max-steps",
            str(arm.max_steps),
            "--max-tokens",
            str(arm.max_tokens),
            "--timeout-seconds",
            str(arm.timeout_seconds),
        ]
        if arm.shell:
            write_private_json(
                task_root / "action-config.json", action_config_json(image_id, options.docker)
            )
            command += ["--action-config", str(task_root / "action-config.json")]
        (task_root / "prompt.txt").write_text(task_prompt(task, arm), encoding="utf-8")
        environment = {**os.environ, API_KEY_VARIABLE: "local-replica-no-key"}
        with open(task_root / "exec.log", "wb") as log:
            exit_code = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=environment,
                timeout=arm.timeout_seconds + 900,
                check=False,
            ).returncode
        if (task_root / "summary.json").exists():
            summary = json.loads((task_root / "summary.json").read_text(encoding="utf-8"))
        patch = collect_workspace_patch(
            workspace, Path(shutil.which("git") or "git"), base_commit, exclude_scratch=True
        )
        error = None
    except (RunError, subprocess.TimeoutExpired, OSError) as failure:
        error = f"{type(failure).__name__}: {failure}"[:1000]
    if not options.keep_workspaces and workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    outcome = Outcome(
        instance_id=task.instance_id,
        arm=arm.name,
        port=port,
        status=summary.get("status") or ("error" if error else "unknown"),
        exit_code=exit_code,
        elapsed_seconds=round(time.monotonic() - started, 1),
        model_steps=summary.get("model_steps"),
        usage=summary.get("usage"),
        patch_bytes=len(patch.encode()),
        changed_paths=summary.get("changed_paths"),
        error=error or _turn_error(summary),
    )
    return outcome, patch


def _turn_error(summary: dict[str, Any]) -> str | None:
    error = summary.get("error")
    return str(error["code"]) if isinstance(error, dict) and "code" in error else None


# ---------------------------------------------------------------- run


def load_tasks(task_list: Path, rows: Path, split: str | None) -> list[Task]:
    by_id = {}
    for line in rows.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        by_id[row["instance_id"]] = row
    document = json.loads(task_list.read_text(encoding="utf-8"))
    tasks = []
    for entry in document["tasks"]:
        if split and entry.get("split", document.get("split")) != split:
            continue
        row = by_id[entry["instance_id"]]
        tasks.append(
            Task(
                entry["instance_id"],
                entry["image"],
                row["repo"],
                row["base_commit"],
                row["problem_statement"],
            )
        )
    return tasks


def finished_instances(results: Path) -> set[str]:
    if not results.exists():
        return set()
    return {json.loads(line)["instance_id"] for line in results.read_text().splitlines() if line}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_list", type=Path)
    parser.add_argument("--rows", type=Path, required=True, help="dataset rows JSONL")
    parser.add_argument("--split", help="only tasks of this split (dev/holdout)")
    parser.add_argument("--arm", choices=sorted(ARMS), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--ports", default="18002,18001")
    parser.add_argument("--per-replica", type=int, default=2)
    parser.add_argument("--limit", type=int, help="run at most this many unfinished tasks")
    parser.add_argument("--only", nargs="*", help="restrict to these instance IDs")
    parser.add_argument("--harnessix", default=str(Path(sys.executable).with_name("harnessix")))
    parser.add_argument("--docker", default=shutil.which("docker") or "/usr/bin/docker")
    parser.add_argument("--keep-workspaces", action="store_true")
    options = parser.parse_args()

    arm = ARMS[options.arm]
    run_root = options.run_root.absolute()
    run_root.mkdir(parents=True, exist_ok=True)
    run_root.chmod(0o755)
    results, predictions = run_root / "results.jsonl", run_root / "predictions.jsonl"
    done = finished_instances(results)
    tasks = [
        t
        for t in load_tasks(options.task_list, options.rows, options.split)
        if t.instance_id not in done and (not options.only or t.instance_id in options.only)
    ]
    tasks = tasks[: options.limit] if options.limit else tasks
    (run_root / "arm.json").write_text(json.dumps(asdict(arm), indent=2) + "\n")

    slots: queue.Queue[int] = queue.Queue()
    for port in [int(p) for p in options.ports.split(",")]:
        for _ in range(options.per_replica):
            slots.put(port)
    lock = threading.Lock()

    def worker(task: Task) -> None:
        port = slots.get()
        try:
            outcome, patch = run_task(task, arm, port, run_root, options)
        finally:
            slots.put(port)
        prediction = {
            "instance_id": task.instance_id,
            "model_name_or_path": f"harnessix-{arm.name}-{MODEL}",
            "model_patch": patch,
        }
        with lock:
            with open(predictions, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(prediction) + "\n")
            with open(results, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(outcome)) + "\n")
        print(
            f"{outcome.instance_id}: {outcome.status} steps={outcome.model_steps} "
            f"patch={outcome.patch_bytes}B {outcome.elapsed_seconds:.0f}s "
            f"{outcome.error or ''}",
            flush=True,
        )

    print(f"arm {arm.name}: {len(tasks)} tasks ({len(done)} already finished)", flush=True)
    with ThreadPoolExecutor(max_workers=slots.qsize()) as pool:
        list(pool.map(worker, tasks))


if __name__ == "__main__":
    main()
