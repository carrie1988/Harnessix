"""Pull relayed SWE-bench images, verify official digests and retag them to official names.

Each task list entry carries the official Docker Hub reference and its manifest-list digest.
An image is accepted only when the digest pulled from the relay registry equals that official
digest; it is then tagged with the official `swebench/...` name so the official evaluator and
Harnessix find it unchanged. Only the standard library and the docker CLI are used.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


def relay_reference(official: str, registry: str, namespace: str) -> str:
    repository_and_tag = official.split("/", 1)[1]  # drop the Docker Hub namespace
    return f"{registry}/{namespace}/{repository_and_tag}"


def docker(*arguments: str) -> str:
    completed = subprocess.run(["docker", *arguments], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def pull_and_verify(task: dict[str, str], registry: str, namespace: str) -> tuple[str, str]:
    """Return (instance_id, status) where status is 'ok' or a failure reason."""
    relay = relay_reference(task["image"], registry, namespace)
    repository = relay.rsplit(":", 1)[0]
    try:
        docker("pull", "--quiet", relay)
        digests = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", relay))
        expected = f"{repository}@{task['digest']}"
        if expected not in digests:
            return task["instance_id"], f"digest mismatch: {digests}"
        docker("tag", relay, task["image"])
    except RuntimeError as error:
        return task["instance_id"], f"docker error: {str(error)[:200]}"
    return task["instance_id"], "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_lists", nargs="+", help="benchmarks/swebench/week1-local/*.json")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--parallel", type=int, default=4)
    args = parser.parse_args()

    tasks = []
    for path in args.task_lists:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        for task in document["tasks"]:
            split = task.get("split", document.get("split"))
            if args.split in ("all", split):
                tasks.append(task)

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        results = list(pool.map(lambda t: pull_and_verify(t, args.registry, args.namespace), tasks))
    failures = [(instance, status) for instance, status in results if status != "ok"]
    print(f"images: {len(results)}  ok: {len(results) - len(failures)}  failed: {len(failures)}")
    for instance, status in failures:
        print(f"FAILED {instance}: {status}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
