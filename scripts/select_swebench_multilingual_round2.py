"""Select the round-2 test and dev subsets of SWE-bench Multilingual with a fixed seed.

The week-1 list (benchmarks/swebench/week1-local/multilingual40.json) is frozen and its holdout
tasks were analysed case by case, so they can no longer show whether a change generalises. This
script draws new tasks from the rest of the dataset with the same rules as the frozen script
(language map, leak filter, official Docker Hub image), and adds a cap per repository.

Per language, the shuffled pool is consumed in this order: test, dev, reserve. A task that cannot
be evaluated (its gold patch does not pass in the offline environment, or its Harbor task
disagrees with the dataset row) is replaced by the first unused reserve of the same language with
the `substitute` command; that is the only allowed change to the list, and it is recorded.

Requires pyarrow, which is not a project dependency. Docker Hub is reached through HTTPS_PROXY:
    HTTPS_PROXY=http://127.0.0.1:7892 python3 scripts/select_swebench_multilingual_round2.py \
        select test-00000-of-00001.parquet benchmarks/swebench/week1-local/multilingual40.json \
        benchmarks/harbor/swebench-multilingual-round2/selection.json
    python3 scripts/select_swebench_multilingual_round2.py \
        substitute benchmarks/harbor/swebench-multilingual-round2/selection.json \
        <instance_id> "<reason>"
"""

import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from select_swebench_multilingual_subset import docker_hub_digest, language_of, leaks_solution

SEED = 20261111
TEST_PER_LANGUAGE = {
    "java": 6,
    "typescript/javascript": 6,
    "go": 6,
    "rust": 3,
    "c/c++": 3,
    "php": 3,
    "ruby": 3,
}
# Together with the ten week-1 dev tasks the dev set has the same language mix as the test set.
NEW_DEV_PER_LANGUAGE = {language: count * 2 // 3 for language, count in TEST_PER_LANGUAGE.items()}
RESERVE_PER_LANGUAGE = 2
MAX_PER_REPOSITORY = 2
SPLITS = ("test", "dev", "reserve")


def image_of(instance_id: str) -> str:
    return "swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_").lower() + ":latest"


def draw(pool: list[dict], quotas: dict[str, int]) -> list[tuple[str, dict, str]]:
    """Fill the splits in order from a shuffled pool, at most MAX_PER_REPOSITORY per split."""
    picked: list[tuple[str, dict, str]] = []
    used: set[str] = set()
    for split in SPLITS:
        per_repository: Counter[str] = Counter()
        count = 0
        for row in pool:
            if count == quotas[split]:
                break
            if row["instance_id"] in used or per_repository[row["repo"]] == MAX_PER_REPOSITORY:
                continue
            digest = docker_hub_digest(row["instance_id"])
            if digest is None:
                used.add(row["instance_id"])
                continue
            used.add(row["instance_id"])
            per_repository[row["repo"]] += 1
            count += 1
            picked.append((split, row, digest))
        if count < quotas[split]:
            raise SystemExit(f"not enough tasks for {split}: {count} of {quotas[split]}")
    return picked


def select(parquet_path: str, week1_path: str, out_path: str) -> None:
    import pyarrow.parquet as pq  # noqa: PLC0415 - only the draw needs the dataset file

    week1 = json.loads(Path(week1_path).read_text(encoding="utf-8"))
    seen = {task["instance_id"] for task in week1["tasks"]}
    rows = pq.read_table(parquet_path).to_pylist()
    by_language: dict[str, list[dict]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: r["instance_id"]):
        if row["instance_id"] not in seen and not leaks_solution(row):
            by_language[language_of(row["repo"])].append(row)
    rng = random.Random(SEED)
    tasks = [
        {**task, "split": "dev", "origin": "week1-dev"}
        for task in week1["tasks"]
        if task["split"] == "dev"
    ]
    for language in TEST_PER_LANGUAGE:
        pool = by_language[language]
        rng.shuffle(pool)
        quotas = {
            "test": TEST_PER_LANGUAGE[language],
            "dev": NEW_DEV_PER_LANGUAGE[language],
            "reserve": RESERVE_PER_LANGUAGE,
        }
        for split, row, digest in draw(pool, quotas):
            tasks.append(
                {
                    "instance_id": row["instance_id"],
                    "repo": row["repo"],
                    "language": language,
                    "split": split,
                    "origin": "round2",
                    "image": image_of(row["instance_id"]),
                    "digest": digest,
                }
            )
    document = {
        "spec": "harnessix.round2.task-list/v1",
        "dataset": week1["dataset"],
        "source_parquet": week1["source_parquet"],
        "source_parquet_sha256": hashlib.sha256(Path(parquet_path).read_bytes()).hexdigest(),
        "selection": {
            "script": "scripts/select_swebench_multilingual_round2.py",
            "seed": SEED,
            "excluded": week1_path,
            "order_per_language": list(SPLITS),
            "max_per_repository_per_split": MAX_PER_REPOSITORY,
            "leak_filter": week1["selection"]["leak_filter"],
            "image_filter": week1["selection"]["image_filter"],
            "substitution": "task cannot be evaluated -> first unused reserve of the language",
        },
        "substitutions": [],
        "tasks": tasks,
    }
    Path(out_path).write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for task in tasks:
        counts[task["split"]][task["language"]] += 1
    print({split: dict(sorted(by.items())) for split, by in counts.items()})


def substitute(selection_path: str, instance_id: str, reason: str) -> None:
    """Drop one task and promote the first unused reserve of its language into its split."""
    path = Path(selection_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    dropped = next(task for task in document["tasks"] if task["instance_id"] == instance_id)
    if dropped["split"] not in ("test", "dev"):
        raise SystemExit(f"{instance_id} is not in the test or dev split")
    reserve = next(
        (
            task
            for task in document["tasks"]
            if task["split"] == "reserve" and task["language"] == dropped["language"]
        ),
        None,
    )
    if reserve is None:
        raise SystemExit(f"no unused {dropped['language']} reserve left")
    reserve["split"] = dropped["split"]
    dropped["split"] = "dropped"
    document.setdefault("substitutions", []).append(
        {"dropped": instance_id, "promoted": reserve["instance_id"], "reason": reason}
    )
    path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"{instance_id} -> {reserve['instance_id']} ({reserve['split']})")


if __name__ == "__main__":
    command, arguments = sys.argv[1], sys.argv[2:]
    {"select": select, "substitute": substitute}[command](*arguments)
