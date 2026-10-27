"""Token-cap counterfactual: rebuild each case's deliverable patch as it stood when cumulative tokens
(model steps + compactions) last stayed within a cap, by replaying the agent's own successful patch calls.
Validation: the uncapped replay must equal the delivered patch (as in the P0-1 replay). No model calls."""
import hashlib, json, os, pathlib, subprocess, sys, tempfile, sqlite3
sys.path.insert(0, "/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix/src")
from harnessix.evals.swebench.predictions import collect_workspace_patch

B = pathlib.Path("/Users/zhangjinhui/Library/Application Support/Harnessix/swebench")
W = pathlib.Path("/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix/benchmarks/swebench")
CACHE = B / "generalization-v45/public-repository-cache"; GIT = pathlib.Path("/usr/bin/git")
RUNS = {"v57p": "agent-9059bcbc2209-v57p", "v61r": "agent-6c2036bce49c-v61r", "v62": "agent-fbc53720e7af-v62"}
CAPS = (1_000_000, 1_500_000)
OUT = pathlib.Path(__file__).with_name("snapshots.json")

def apply_ops(root, files):
    for item in files:
        path = root / item["path"]; op = item.get("operation") or ("edit" if item.get("edits") else "replace")
        if op == "delete": path.unlink()
        elif op in ("create", "replace"):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes((item.get("content") or "").encode())
            if item.get("mode") is not None: os.chmod(path, int(item["mode"]))
        else:
            text = path.read_bytes().decode(); spans = []
            for e in item["edits"]:
                s = text.find(e["old_text"]); assert s >= 0 and text.find(e["old_text"], s + 1) < 0
                spans.append((s, s + len(e["old_text"]), e["new_text"]))
            out, pos = [], 0
            for s, t, new in sorted(spans): out += [text[pos:s], new]; pos = t
            path.write_bytes(("".join(out) + text[pos:]).encode())

def snapshot(repo, base, ops):
    with tempfile.TemporaryDirectory(prefix="hx-cap-") as tmp:
        root = pathlib.Path(tmp) / "w"
        subprocess.run([str(GIT), "clone", "-q", "--shared", "--no-checkout", str(CACHE / (hashlib.sha256(repo.encode()).hexdigest() + ".git")), str(root)], check=True)
        subprocess.run([str(GIT), "-C", str(root), "checkout", "-q", "--detach", base], check=True)
        for files in ops: apply_ops(root, files)
        return collect_workspace_patch(root, GIT, base, exclude_scratch=True)

def timeline(db, compactions):
    """Successful patch calls, each tagged with cumulative tokens of the step that issued it."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); calls = {}; ops = []; cum = 0; pending = list(compactions)
    for (raw,) in con.execute("select event_json from agent_events order by sequence"):
        p = json.loads(raw)["payload"]
        if p["type"] == "usage_recorded": u = p["usage"]; cum += (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
        elif p["type"] == "compaction_summarized" and pending: c = pending.pop(0); cum += (c["input_tokens"] or 0) + (c["output_tokens"] or 0)
        elif p["type"] == "item_finished":
            c = p["content"]
            if c["kind"] == "tool_call": calls[c["call_id"]] = c
            elif c["kind"] == "tool_result" and c["outcome"] == "succeeded" and calls.get(c["call_id"], {}).get("tool") == "apply_patch_batch":
                files = calls[c["call_id"]]["arguments"]["files"]; ops.append((cum, json.loads(files) if isinstance(files, str) else files))
    return ops, cum

rows = []
for run, agent in RUNS.items():
    summary = {c["instance_id"]: c for c in json.loads((W / f"generalization-25-{run}/execution-summary.json").read_text())["cases"]}
    for cdir in sorted((B / "runs" / agent / "cases").glob("case-*")):
        st = json.loads((cdir / "case-state.json").read_text()); iid = st["instance_id"]; s = summary[iid]
        repo = iid.split("__")[0] + "/" + iid.split("__")[1].rsplit("-", 1)[0]
        sealed = cdir / "sealed-workspace.json"; delivered = json.loads(sealed.read_text())["patch"] if sealed.exists() else ""
        ops, cum = timeline(cdir / "run/session.sqlite", s["compaction_attempts"])
        total = s["input_tokens"] + s["output_tokens"]
        row = {"run": run, "id": iid, "total_tokens": total, "replayed_tokens": cum, "cost": float(s["known_cost_upper_amount"]),
               "final_matches_delivered": snapshot(repo, st["base_commit"], [f for _, f in ops]) == delivered, "caps": {}}
        for cap in CAPS:
            if total > cap:
                row["caps"][str(cap)] = snapshot(repo, st["base_commit"], [f for t, f in ops if t <= cap])
        rows.append(row); print(run, iid, total, cum == total, row["final_matches_delivered"], flush=True)
OUT.write_text(json.dumps(rows, ensure_ascii=False))
print("cases", len(rows), "token totals match", sum(r["replayed_tokens"] == r["total_tokens"] for r in rows), "final==delivered", sum(r["final_matches_delivered"] for r in rows))
