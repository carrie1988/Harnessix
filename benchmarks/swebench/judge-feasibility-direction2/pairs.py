"""Collect decisive candidate pairs (same case, one officially resolved, one not; both non-empty)."""
import json, glob, pathlib, collections
B = pathlib.Path("/Users/zhangjinhui/Library/Application Support/Harnessix/swebench")
W = pathlib.Path("/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix/benchmarks/swebench")
OUT = pathlib.Path(__file__).with_name("pairs.json")
cands = collections.defaultdict(list)
for off in sorted((B / "runs").glob("official-*")):
    sha, run = off.name[len("official-"):].split("-", 1)
    base_run = run.split("-partial")[0].split("-attempted")[0]
    agent = B / "runs" / f"agent-{sha}-{base_run}"
    results = list(off.glob("logs/evaluation/*/results.json"))
    if not agent.exists() or not results: continue
    res = json.loads(results[0].read_text()); ok = set(res["resolved_ids"]); sub = set(res["submitted_ids"])
    for sealed in agent.glob("cases/case-*/sealed-workspace.json"):
        s = json.loads(sealed.read_text())
        if s["instance_id"] in sub and s["patch"].strip():
            cands[s["instance_id"]].append({"source": base_run, "patch": s["patch"], "resolved": s["instance_id"] in ok})
cf = W / "counterfactual-p0-first-review"
cfres = set(json.loads((cf / "official-first-review.json").read_text())["resolved_ids"])
for line in (cf / "predictions-first-review.jsonl").read_text().splitlines():
    r = json.loads(line)
    cands[r["instance_id"]].append({"source": "first-review", "patch": r["model_patch"], "resolved": r["instance_id"] in cfres})
issue = {}
for f in glob.glob(str(B / "generalization-v*/candidates*.jsonl")):
    for l in open(f):
        if l.strip(): d = json.loads(l); issue.setdefault(d["instance_id"], d["problem_statement"])
for plan in W.glob("*/plan.json"):
    for case in json.loads(plan.read_text())["cases"]: issue.setdefault(case["instance_id"], case["problem_statement"])
pairs = []
for iid, cs in sorted(cands.items()):
    uniq = {}
    for c in cs: uniq.setdefault(c["patch"].strip(), c)
    good = [c for c in uniq.values() if c["resolved"]]; bad = [c for c in uniq.values() if not c["resolved"]]
    if good and bad: pairs.append({"instance_id": iid, "issue": issue[iid], "good": good[0], "bad": bad[0]})
OUT.write_text(json.dumps(pairs, ensure_ascii=False))
print("instances with candidates:", len(cands), "| decisive pairs:", len(pairs))
