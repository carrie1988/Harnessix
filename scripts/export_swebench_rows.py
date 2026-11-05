"""Export pinned SWE-bench rows for the frozen week-1 task lists.

Writes two JSONL files: the public fields the agent may see (instance_id, repo, base_commit,
problem_statement) and the complete rows for the official evaluator, which then reads a local
file instead of the network. Runs in the evaluator environment (needs huggingface_hub and
pyarrow); set HF_ENDPOINT to use a mirror.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as parquet  # type: ignore[import-not-found]
from huggingface_hub import hf_hub_download  # type: ignore[import-not-found]

PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True, help="e.g. SWE-bench/SWE-bench_Verified")
    parser.add_argument("--revision", required=True, help="pinned dataset commit")
    parser.add_argument("--parquet", default="data/test-00000-of-00001.parquet")
    parser.add_argument("--sha256", help="expected SHA-256 of the parquet file")
    parser.add_argument("--task-lists", nargs="+", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--full", type=Path, required=True)
    arguments = parser.parse_args()

    wanted = {
        task["instance_id"]
        for path in arguments.task_lists
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }
    local = hf_hub_download(
        arguments.dataset, arguments.parquet, repo_type="dataset", revision=arguments.revision
    )
    digest = hashlib.sha256(Path(local).read_bytes()).hexdigest()
    if arguments.sha256 and digest != arguments.sha256:
        raise SystemExit(f"parquet sha256 {digest} != expected {arguments.sha256}")
    rows = [row for row in parquet.read_table(local).to_pylist() if row["instance_id"] in wanted]
    missing = wanted - {row["instance_id"] for row in rows}
    if missing:
        raise SystemExit(f"instances missing from dataset: {sorted(missing)}")
    with (
        arguments.full.open("w", encoding="utf-8") as full,
        arguments.public.open("w", encoding="utf-8") as public,
    ):
        for row in sorted(rows, key=lambda item: item["instance_id"]):
            full.write(json.dumps(row, default=str) + "\n")
            public.write(json.dumps({field: row[field] for field in PUBLIC_FIELDS}) + "\n")
    print(f"{arguments.dataset}@{arguments.revision} sha256={digest} rows={len(rows)}")


if __name__ == "__main__":
    main()
