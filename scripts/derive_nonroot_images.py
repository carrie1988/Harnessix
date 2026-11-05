"""Derive SWE-bench instance images whose /root toolchain caches the sandbox user can read.

Harnessix runs commands as an unprivileged user. Several Multilingual images keep offline caches
under /root (Go module cache, corepack, Gradle/Maven repositories), which is mode 0700 there.
For each task this adds one layer that only grants world read/traverse on /root, tags it
`<official repository>:nonroot` and labels it with the base image ID. Official evaluation keeps
using the unmodified images. Idempotent: an existing derivation of the same base is reused.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LABEL = "harnessix.derived-from"


def docker(*arguments: str, check: bool = True) -> str:
    completed = subprocess.run(["docker", *arguments], capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip()[-300:])
    return completed.stdout.strip()


def derived_name(image: str) -> str:
    return image.rsplit(":", 1)[0] + ":nonroot"


def derive(image: str) -> tuple[str, str]:
    """Return (derived image ID, status)."""

    base = docker("image", "inspect", "--format", "{{.Id}}", image)
    target = derived_name(image)
    existing = docker(
        "image",
        "inspect",
        "--format",
        f'{{{{index .Config.Labels "{LABEL}"}}}}',
        target,
        check=False,
    )
    if existing == base:
        return docker("image", "inspect", "--format", "{{.Id}}", target), "reused"
    name = "hx-derive-" + base.split(":", 1)[1][:16]
    docker("rm", "-f", name, check=False)
    docker(
        "run",
        "--name",
        name,
        "--user",
        "0:0",
        "--network",
        "none",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        "chmod a+rx /root && chmod -R a+rX /root",
    )
    try:
        docker("commit", "--change", f"LABEL {LABEL}={base}", name, target)
    finally:
        docker("rm", "-f", name, check=False)
    return docker("image", "inspect", "--format", "{{.Id}}", target), "derived"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_lists", nargs="+", type=Path)
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--manifest", type=Path, required=True, help="write instance -> image IDs")
    arguments = parser.parse_args()

    tasks = []
    for path in arguments.task_lists:
        document = json.loads(path.read_text(encoding="utf-8"))
        for task in document["tasks"]:
            if arguments.split in ("all", task.get("split", document.get("split"))):
                tasks.append(task)

    def one(task: dict[str, str]) -> tuple[str, dict[str, str]]:
        try:
            image_id, status = derive(task["image"])
            return task["instance_id"], {
                "image": derived_name(task["image"]),
                "id": image_id,
                "status": status,
            }
        except RuntimeError as error:
            return task["instance_id"], {"status": f"failed: {error}"}

    with ThreadPoolExecutor(max_workers=arguments.parallel) as pool:
        manifest = dict(pool.map(one, tasks))
    arguments.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    failed = {key: value for key, value in manifest.items() if value["status"].startswith("failed")}
    print(f"derived {len(manifest) - len(failed)}/{len(manifest)}; failed: {sorted(failed)}")


if __name__ == "__main__":
    main()
