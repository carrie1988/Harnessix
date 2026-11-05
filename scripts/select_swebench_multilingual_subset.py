"""Select a stratified, leak-filtered subset of SWE-bench Multilingual with a fixed seed.

Requires pyarrow, which is not a project dependency:
    uv run --no-project --with pyarrow python scripts/select_swebench_multilingual_subset.py \
        test-00000-of-00001.parquet benchmarks/swebench/week1-local/multilingual40.json
"""

import json
import random
import re
import sys
import urllib.request
from collections import defaultdict

import pyarrow.parquet as pq

SEED = 20261105
DEV_PER_LANGUAGE = {
    "java": 2,
    "typescript/javascript": 2,
    "go": 2,
    "rust": 1,
    "c/c++": 1,
    "php": 1,
    "ruby": 1,
}
HOLDOUT_TARGET = 30

# Repository → language, from the SWE-bench Multilingual repository list.
LANGUAGE_HINTS = {
    "java": [
        "apache/druid",
        "apache/lucene",
        "google/gson",
        "javaparser/javaparser",
        "projectlombok/lombok",
        "reactivex/rxjava",
        "mockito/mockito",
        "fasterxml/jackson",
        "elastic/logstash",
    ],
    "typescript/javascript": [
        "axios/axios",
        "babel/babel",
        "facebook/docusaurus",
        "immutable-js/immutable-js",
        "mrdoob/three.js",
        "preactjs/preact",
        "vuejs/core",
        "sveltejs/svelte",
        "iamkun/dayjs",
        "expressjs/express",
        "prettier/prettier",
    ],
    "go": [
        "caddyserver/caddy",
        "gin-gonic/gin",
        "gohugoio/hugo",
        "hashicorp/terraform",
        "prometheus/prometheus",
        "go-gorm/gorm",
        "spf13/cobra",
        "zeromicro/go-zero",
        "cli/cli",
    ],
    "rust": [
        "astral-sh/ruff",
        "burntsushi/ripgrep",
        "nushell/nushell",
        "sharkdp/bat",
        "tokio-rs/axum",
        "tokio-rs/tokio",
        "uutils/coreutils",
        "sharkdp/fd",
        "rust-lang/mdbook",
    ],
    "c/c++": [
        "fmtlib/fmt",
        "jqlang/jq",
        "micropython/micropython",
        "nlohmann/json",
        "redis/redis",
        "valkey-io/valkey",
        "curl/curl",
        "systemd/systemd",
    ],
    "php": [
        "laravel/framework",
        "php-cs-fixer/php-cs-fixer",
        "phpoffice/phpspreadsheet",
        "briannesbitt/carbon",
        "symfony/symfony",
    ],
    "ruby": [
        "faker-ruby/faker",
        "fastlane/fastlane",
        "fluent/fluentd",
        "jekyll/jekyll",
        "jordansissel/fpm",
        "rubocop/rubocop",
        "rails/rails",
    ],
}


def language_of(repo: str) -> str:
    for language, repos in LANGUAGE_HINTS.items():
        if repo.lower() in repos:
            return language
    return "unknown"


def leaks_solution(row: dict) -> bool:
    """True when the issue text links to its own fix PR or embeds a diff."""
    statement = row["problem_statement"] or ""
    number = row["instance_id"].rsplit("-", 1)[-1]
    own_pr = re.search(
        rf"github\.com/{re.escape(row['repo'])}/pull/{number}\b", statement, re.IGNORECASE
    )
    embedded_diff = re.search(r"^diff --git |^@@ -\d+", statement, re.MULTILINE)
    return bool(own_pr or embedded_diff)


def docker_hub_digest(instance_id: str) -> str | None:
    repository = "swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_").lower()
    try:
        tag = json.load(
            urllib.request.urlopen(
                f"https://hub.docker.com/v2/repositories/{repository}/tags/latest", timeout=30
            )
        )
    except Exception:
        return None
    return tag.get("digest")


def main(parquet_path: str, out_path: str) -> None:
    rows = pq.read_table(parquet_path).to_pylist()
    by_language: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_language[language_of(row["repo"])].append(row)
    print("languages:", {k: len(v) for k, v in sorted(by_language.items())})

    rng = random.Random(SEED)
    selection = []
    holdout_share = {
        lang: n * 3 for lang, n in DEV_PER_LANGUAGE.items()
    }  # 3:1 holdout:dev per language
    for language, dev_count in DEV_PER_LANGUAGE.items():
        pool = [r for r in by_language.get(language, []) if not leaks_solution(r)]
        rng.shuffle(pool)
        wanted = dev_count + holdout_share[language]
        picked = []
        for row in pool:
            if len(picked) == wanted:
                break
            digest = docker_hub_digest(row["instance_id"])
            if digest:
                picked.append((row, digest))
        for index, (row, digest) in enumerate(picked):
            selection.append(
                {
                    "instance_id": row["instance_id"],
                    "repo": row["repo"],
                    "language": language,
                    "split": "dev" if index < dev_count else "holdout",
                    "image": "swebench/sweb.eval.x86_64."
                    + row["instance_id"].replace("__", "_1776_").lower()
                    + ":latest",
                    "digest": digest,
                }
            )
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({"seed": SEED, "tasks": selection}, handle, indent=1)
    counts = defaultdict(lambda: defaultdict(int))
    for task in selection:
        counts[task["language"]][task["split"]] += 1
    print("selected:", len(selection), {k: dict(v) for k, v in counts.items()})
    print("unknown-language repos:", sorted({r["repo"] for r in by_language.get("unknown", [])}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
