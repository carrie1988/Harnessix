"""Freeze the d39 development smoke plan and the paired v63 fresh plans (run from the source root, no model calls).

d39: 25 previously used development cases (the 20 whose images the cap counterfactual built plus 5 from the d38 pool)
for a mechanics smoke of run-config v39 on Bailian deepseek-v4-pro-0813; only its first cases are executed.
v63t / v63n: the same 25 fresh cases (same candidates and salt) disjoint from all 325 used, frozen with thinking
enabled (v39) and disabled (v37) respectively; selection uses public fields only."""
import glob
import json
import sys
from pathlib import Path

from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)

B = Path("/Users/zhangjinhui/Library/Application Support/Harnessix/swebench")
OUT = Path("benchmarks/swebench")
REVISION = __import__("subprocess").run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
COMMON = dict(
    dataset_name="SWE-bench/SWE-bench_Verified",
    dataset_revision="78f471bf655a3137b2e8a75af1501690ec009ec3",
    dataset_artifact_sha256="030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25",
    split="test",
    harnessix_revision=REVISION,
    swebench_revision="02e7a74ffd0b707aab73d203fe87bdc7c76afc8e",
    swebench_package_version="5.0.2",
    task_repo_revision="3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d",
    model_name="harnessix-deepseek-v4-pro-0813-bailian",
    task_budget=SweBenchTaskBudget(
        max_steps=100, max_tokens=2_000_000, timeout_seconds=2700.0, max_output_chars=65536, max_tool_calls_per_step=8
    ),
)


def model(thinking):
    return SweBenchModelConfig(
        provider="openai_chat",
        parameters=(
            SweBenchModelParameter(name="parallel_tool_calls", value=True),
            SweBenchModelParameter(name="thinking", value=thinking),
        ),
    )


def write(directory, plan):
    path = OUT / directory / "plan.json"
    path.parent.mkdir(parents=True, exist_ok=False)
    path.write_text(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    return path


rows = {}
for f in sorted(glob.glob(str(B / "generalization-v*/candidates*.jsonl"))):
    for line in open(f):
        if line.strip():
            row = json.loads(line)
            rows.setdefault(row["instance_id"], row)

cap = [c["instance_id"] for c in json.loads((B / "cap-counterfactual/cases.json").read_text())["cases"]]
extra = ["django__django-15916", "django__django-14017", "django__django-16899", "django__django-12262", "django__django-10097"]
dev_ids = cap + extra
assert len(set(dev_ids)) == 25 and all(i in rows for i in dev_ids)
d39 = build_swebench_preregistration_plan(
    [rows[i] for i in dev_ids], **COMMON, model_config=model("enabled"), salt="harnessix-swebench-dev-d39-full-thinking-smoke-2026-10-27"
)

fresh = [json.loads(l) for l in open(B / "generalization-v63/candidates-excluding-used.jsonl") if l.strip()]
excluded = set(json.loads((B / "generalization-v63/excluded-ids.json").read_text())["excluded_instance_ids"])
assert len(fresh) == 175 and len(excluded) == 325 and not {r["instance_id"] for r in fresh} & excluded
salt = "harnessix-swebench-verified-generalization-25-v63-2026-10-27"
# 新鲜两臂单题上限1M：75项反事实中1M截断快照的官方通过不低于2M（45 vs 44/75），成本-30%。
FRESH = {**COMMON, "task_budget": COMMON["task_budget"].model_copy(update={"max_tokens": 1_000_000})}
v63t = build_swebench_preregistration_plan(fresh, **FRESH, model_config=model("enabled"), salt=salt)
v63n = build_swebench_preregistration_plan(fresh, **FRESH, model_config=model("disabled"), salt=salt)
assert [c.instance_id for c in v63t.cases] == [c.instance_id for c in v63n.cases]
assert not {c.instance_id for c in v63t.cases} & excluded

DEV = sys.argv[1] if len(sys.argv) > 1 else "deepseek-dev-d39"
if not (OUT / DEV / "plan.json").exists():
    write(DEV, d39)
write("generalization-25-v63t", v63t)
write("generalization-25-v63n", v63n)
for directory in ("generalization-25-v63t", "generalization-25-v63n"):
    (OUT / directory / "exclusion-audit.json").write_text(
        json.dumps({"excluded_instance_ids": sorted(excluded)}, indent=2) + "\n"
    )
print(json.dumps({
    "revision": REVISION,
    "d39": d39.plan_sha256[:12], "d39_first": [c.instance_id for c in d39.cases[:4]],
    "v63t": v63t.plan_sha256[:12], "v63n": v63n.plan_sha256[:12],
    "v63_repos": sorted({c.repo for c in v63t.cases}),
    "v63_counts": {r: sum(c.repo == r for c in v63t.cases) for r in sorted({c.repo for c in v63t.cases})},
}, ensure_ascii=False))
