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
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harnessix.candidates.judge import break_tie, tied_leaders
from harnessix.candidates.selection import (
    Candidate,
    CheckResult,
    CommandRecord,
    rank,
    select_checks,
)
from harnessix.candidates.verification import cross_check
from harnessix.evals.swebench.predictions import collect_workspace_patch
from harnessix.product_config.action_contracts import (
    ProductActionConfigV1,
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
    # Independent runs per task; more than one adds cross-checked selection of a single patch.
    candidates: int = 1


ARMS = {
    # Product defaults before week 1: 16 steps, 100K tokens, 120 s, no command execution.
    "A0": Arm("A0", shell=False, max_steps=16, max_tokens=100_000, timeout_seconds=120),
    # New product turn budget, still without command execution.
    "A1": Arm("A1", shell=False, max_steps=100, max_tokens=1_000_000, timeout_seconds=3_600),
    # New budget plus the generic container command in the instance image.
    "A2": Arm("A2", shell=True, max_steps=100, max_tokens=1_000_000, timeout_seconds=3_600),
    # A2 four times per task, then one patch chosen by cross-running the candidates' own checks.
    # Candidate c0 alone is an ordinary A2 sample.
    "A2x4": Arm(
        "A2x4",
        shell=True,
        max_steps=100,
        max_tokens=1_000_000,
        timeout_seconds=3_600,
        candidates=4,
    ),
}

SHELL_ENVIRONMENT_NOTE = """
Command environment: every exec_command call starts a fresh container from this task's own
image with network disabled. The working directory /testbed is a disposable writable copy of
the repository that includes your patches: builds, caches and scratch files may be written
there or in /tmp, and are discarded when the command ends. Edit source only with the patch
tools. The project's toolchain and dependencies are already installed; work offline.
"""

# Per-language notes and fixed environment presets for the instance images (no secrets).
_COMMON_ENVIRONMENT = (("HOME", "/tmp"), ("XDG_CACHE_HOME", "/tmp/.cache"))
LANGUAGE_ENVIRONMENT: dict[str, tuple[tuple[str, str], ...]] = {
    "python": (
        (
            "PATH",
            "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:"
            "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
    ),
    "go": (
        ("GOCACHE", "/tmp/go-build"),
        ("GOFLAGS", "-mod=mod"),
        ("GOMODCACHE", "/root/go/pkg/mod"),
        ("GOPROXY", "off"),
        ("GOSUMDB", "off"),
        ("GOTOOLCHAIN", "local"),
    ),
    "rust": (("CARGO_NET_OFFLINE", "true"),),
    "typescript/javascript": (
        ("COREPACK_HOME", "/root/.cache/node/corepack"),
        ("YARN_CACHE_FOLDER", "/tmp/.yarn-cache"),
        ("npm_config_cache", "/tmp/.npm"),
    ),
    "java": (
        ("GRADLE_RO_DEP_CACHE", "/root/.gradle/caches"),
        ("GRADLE_USER_HOME", "/tmp/.gradle"),
    ),
}
LANGUAGE_NOTES = {
    "python": "python, pip and pytest resolve to the project's testbed environment.",
    "go": "Go modules resolve offline from the image's module cache.",
    "rust": "cargo runs offline; the image's target directory already holds a build.",
}


def environment_presets(language: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(_COMMON_ENVIRONMENT + LANGUAGE_ENVIRONMENT.get(language, ())))


@dataclass(frozen=True)
class Task:
    instance_id: str
    image: str
    repo: str
    base_commit: str
    problem_statement: str
    language: str = "python"


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
    upstream_modes = _tracked_modes(git, task.base_commit)
    for nested in sorted(directory.rglob(".git"), key=lambda path: len(path.parts), reverse=True):
        if nested.is_dir() and not nested.is_symlink():
            shutil.rmtree(nested)
        else:
            nested.unlink()

    normalize_modes(directory, upstream_modes)
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
    return image_id, _run([*git, "rev-parse", "HEAD"])


def _tracked_modes(git: list[str], commit: str) -> dict[str, int]:
    """Upstream file modes at the base commit (Git records only 100644 and 100755)."""

    listing = subprocess.run(
        [*git, "ls-tree", "-r", "-z", commit], capture_output=True, check=True
    ).stdout
    modes = {}
    for entry in listing.split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            modes[path.decode("utf-8", "surrogateescape")] = int(metadata.split(b" ", 1)[0], 8)
    return modes


def normalize_modes(directory: Path, upstream_modes: dict[str, int]) -> None:
    """Restore Git-style 0644/0755 modes; the images make every file 0777.

    Harnessix workspace transactions edit only 0644/0755 files, and the sandbox user needs
    world read and traverse access. Untracked build outputs keep their owner execute bit.
    """

    for root, directories, files in os.walk(directory):
        for name in directories:
            path = Path(root, name)
            if not path.is_symlink():
                path.chmod(0o755)
        for name in files:
            path = Path(root, name)
            if path.is_symlink():
                continue
            relative = path.relative_to(directory).as_posix()
            mode = upstream_modes.get(relative)
            executable = mode == 0o100755 if mode is not None else path.stat().st_mode & 0o100
            path.chmod(0o755 if executable else 0o644)
    directory.chmod(0o755)


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


def action_config_json(image_id: str, docker: str, language: str) -> str:
    profile = build_product_process_profile(
        profile_id="testbed",
        version="swebench-instance-image-v2",
        description="在本任务的SWE-bench实例镜像中运行命令（/testbed为工作区一次性可写快照）",
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
        workspace_mode="snapshot",
        environment=environment_presets(language),
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
        + (LANGUAGE_NOTES.get(task.language, "") + "\n" if arm.shell else "")
        + f"\nRepository: {task.repo}\n\nProblem statement:\n{task.problem_statement}"
    )


def sandbox_image(task: Task, image_id: str) -> str:
    """Use the derived image whose /root caches the sandbox user can read, when one exists."""

    derived = task.image.rsplit(":", 1)[0] + ":nonroot"
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", derived],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else image_id


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
    task: Task,
    arm: Arm,
    port: int,
    task_root: Path,
    options: argparse.Namespace,
    *,
    keep_workspace: bool,
) -> tuple[Outcome, str]:
    if task_root.exists():
        shutil.rmtree(task_root)  # a previous attempt died before recording a result
    task_root.mkdir(parents=True)
    # The sandbox user must traverse every directory below the run root.
    for directory in (task_root, *task_root.parents):
        if directory == options.run_root or not directory.is_relative_to(options.run_root):
            break
        directory.chmod(0o755)
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
            command_image = sandbox_image(task, image_id)
            (task_root / "command-image.txt").write_text(command_image + "\n")
            write_private_json(
                task_root / "action-config.json",
                action_config_json(command_image, options.docker, task.language),
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
    if not keep_workspace and workspace.exists():
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
    (task_root / "patch.diff").write_text(patch, encoding="utf-8")
    (task_root / "outcome.json").write_text(json.dumps(asdict(outcome)) + "\n", encoding="utf-8")
    return outcome, patch


def select_candidate(
    task: Task, task_root: Path, outcomes: list[Outcome], options: argparse.Namespace
) -> tuple[str, dict[str, Any]]:
    """Cross-run the candidates' own successful checks on every candidate and pick one patch."""

    candidates, workspaces = [], {}
    for index, outcome in enumerate(outcomes):
        root = task_root / f"c{index}"
        summary_path = root / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        commands = tuple(
            CommandRecord(item["cmd"], item.get("outcome"), item.get("returncode"))
            for item in summary.get("commands", [])
            if isinstance(item.get("cmd"), str)
        )
        patch = (root / "patch.diff").read_text(encoding="utf-8")
        candidates.append(Candidate(f"c{index}", patch, outcome.status, commands))
        if patch and (root / "workspace").is_dir():
            workspaces[f"c{index}"] = root / "workspace"
    checks = select_checks(candidates)
    runs = []
    if checks and len({c.patch_sha256 for c in candidates if c.patch}) > 1:
        config = ProductActionConfigV1.model_validate_json(
            (task_root / "c0" / "action-config.json").read_text(encoding="utf-8")
        )
        runs = cross_check(config.process_profiles[0], workspaces, checks, task_root / "verify")
    matrix: dict[str, dict[int, CheckResult]] = {}
    for run in runs:
        matrix.setdefault(run.candidate_id, {})[run.check_index] = run.result
    selection = rank(candidates, checks, matrix)
    selected, reason, judgements = selection.selected, selection.reason, []
    leaders = tied_leaders(selection.ranking, candidates)
    if len(leaders) > 1:
        port = int(options.ports.split(",")[0])
        winner, judgements = break_tie(task.problem_statement, leaders, judge_model(port))
        if winner is not None:
            selected = winner
            reason += f"；与{len(leaders) - 1}个并列补丁两两评审，双顺序一致选择{winner}"
        else:
            reason += "；两两评审未形成一致结论，保持原排序"
    record = {
        "instance_id": task.instance_id,
        "selected": selected,
        "reason": reason,
        "judgements": [asdict(item) for item in judgements],
        "checks": [asdict(check) for check in selection.checks],
        "runs": [asdict(run) for run in runs],
        "ranking": [asdict(item) for item in selection.ranking],
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "status": c.status,
                "patch_sha256": c.patch_sha256,
                "patch_bytes": len(c.patch.encode()),
                "commands": len(c.commands),
            }
            for c in candidates
        ],
    }
    (task_root / "selection.json").write_text(json.dumps(record, indent=2) + "\n")
    chosen = next((c.patch for c in candidates if c.candidate_id == selected), "")
    return chosen, record


def judge_model(port: int) -> Callable[[str], str]:
    """Pairwise judge on the same local model: one short, deterministic, thinking-off answer."""

    def ask(prompt: str) -> str:
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                answer = json.loads(response.read())["choices"][0]["message"]["content"]
        except (OSError, ValueError, KeyError, IndexError):
            return ""
        return str(answer or "")

    return ask


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
                entry.get("language", "python"),
            )
        )
    return tasks


def source_revision() -> dict[str, object]:
    """The Harnessix revision under evaluation; a dirty tree is recorded, not hidden."""

    repository = Path(__file__).resolve().parent.parent
    git = ["git", "-c", "safe.directory=*", "-C", str(repository)]
    status = _run([*git, "status", "--porcelain", "--untracked-files=no"])
    return {"harnessix_revision": _run([*git, "rev-parse", "HEAD"]), "dirty": bool(status)}


def wait_until_healthy(port: int) -> None:
    """Do not start a task on a dead replica; it would only record an infrastructure failure."""

    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10):
                return
        except OSError:
            print(f"replica :{port} unhealthy; retrying in 30 s", flush=True)
            time.sleep(30)


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
    run_root = options.run_root = options.run_root.absolute()
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
    (run_root / "arm.json").write_text(
        json.dumps({**asdict(arm), **source_revision()}, indent=2) + "\n"
    )

    slots: queue.Queue[int] = queue.Queue()
    for port in [int(p) for p in options.ports.split(",")]:
        for _ in range(options.per_replica):
            slots.put(port)
    lock = threading.Lock()
    pending: dict[str, dict[int, Outcome]] = {}

    def append(path: Path, record: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def prediction(task: Task, patch: str) -> dict[str, str]:
        name = f"harnessix-{arm.name}-{MODEL}"
        return {"instance_id": task.instance_id, "model_name_or_path": name, "model_patch": patch}

    def run_candidate(task: Task, index: int) -> Outcome:
        root = run_root / "tasks" / task.instance_id
        root = root / f"c{index}" if arm.candidates > 1 else root
        if arm.candidates > 1 and (root / "outcome.json").exists():
            return Outcome(**json.loads((root / "outcome.json").read_text()))  # resumed
        port = slots.get()
        wait_until_healthy(port)
        try:
            outcome, _ = run_task(
                task,
                arm,
                port,
                root,
                options,
                keep_workspace=options.keep_workspaces or arm.candidates > 1,
            )
        finally:
            slots.put(port)
        print(
            f"{task.instance_id}{f' c{index}' if arm.candidates > 1 else ''}: {outcome.status} "
            f"steps={outcome.model_steps} patch={outcome.patch_bytes}B "
            f"{outcome.elapsed_seconds:.0f}s {outcome.error or ''}",
            flush=True,
        )
        return outcome

    def finish(task: Task, outcomes: list[Outcome]) -> None:
        """Record one prediction per task; with candidates, also every candidate's own patch."""

        task_root = run_root / "tasks" / task.instance_id
        if arm.candidates == 1:
            patch = (task_root / "patch.diff").read_text(encoding="utf-8")
            record: dict[str, Any] = asdict(outcomes[0])
        else:
            patch, selection = select_candidate(task, task_root, outcomes, options)
            record = {
                "instance_id": task.instance_id,
                "arm": arm.name,
                "selected": selection["selected"],
                "reason": selection["reason"],
                "candidates": [asdict(outcome) for outcome in outcomes],
            }
            if not options.keep_workspaces:
                for index in range(arm.candidates):
                    shutil.rmtree(task_root / f"c{index}" / "workspace", ignore_errors=True)
            print(f"{task.instance_id}: selected {selection['selected']} ({selection['reason']})")
        with lock:
            for index, _ in enumerate(outcomes if arm.candidates > 1 else []):
                candidate_patch = (task_root / f"c{index}" / "patch.diff").read_text()
                append(run_root / f"predictions-c{index}.jsonl", prediction(task, candidate_patch))
            append(predictions, prediction(task, patch))
            append(results, record)

    def job(item: tuple[Task, int]) -> None:
        task, index = item
        outcome = run_candidate(task, index)
        with lock:
            collected = pending.setdefault(task.instance_id, {})
            collected[index] = outcome
            complete = len(collected) == arm.candidates
        if complete:
            finish(task, [collected[i] for i in range(arm.candidates)])

    jobs = [(task, index) for task in tasks for index in range(arm.candidates)]
    print(
        f"arm {arm.name}: {len(tasks)} tasks x {arm.candidates} ({len(done)} already finished)",
        flush=True,
    )
    # Extra workers let candidate selection (CPU-only containers) overlap with model slots.
    with ThreadPoolExecutor(max_workers=slots.qsize() + 2) as pool:
        list(pool.map(job, jobs))


if __name__ == "__main__":
    main()
