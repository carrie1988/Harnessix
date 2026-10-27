"""Counterfactual patch reconstruction: replay successful patch operations on a clean checkout.

Snapshots per case: first_review (state at the first successful git_review), last_review, final.
Validation: the 'final' replay must equal the sealed delivered patch byte for byte.
Read-only with respect to run evidence; works in a scratch directory only.
"""
import hashlib, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile
sys.path.insert(0, "/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix/src")
from harnessix.evals.swebench.predictions import collect_workspace_patch

B = pathlib.Path("/Users/zhangjinhui/Library/Application Support/Harnessix/swebench")
CACHE = B / "generalization-v45/public-repository-cache"
GIT = pathlib.Path("/usr/bin/git")
RUNS = {"v57p": "agent-9059bcbc2209-v57p", "v61r": "agent-6c2036bce49c-v61r", "v62": "agent-fbc53720e7af-v62"}
OUT = pathlib.Path(sys.argv[1])

def cache_for(repo):
    return CACHE / (hashlib.sha256(repo.encode()).hexdigest() + ".git")

def apply_ops(root, files):
    for item in files:
        path = root / item["path"]
        op = item.get("operation") or ("edit" if item.get("edits") else "replace")
        if op == "delete":
            path.unlink()
        elif op in ("create", "replace"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((item.get("content") or "").encode())
            if item.get("mode") is not None:
                os.chmod(path, int(item["mode"]))
        elif op == "edit":
            text = path.read_bytes().decode()
            spans = []
            for e in item["edits"]:
                start = text.find(e["old_text"]); assert start >= 0 and text.find(e["old_text"], start + 1) < 0
                spans.append((start, start + len(e["old_text"]), e["new_text"]))
            out, pos = [], 0
            for s, t, new in sorted(spans):
                out.append(text[pos:s]); out.append(new); pos = t
            out.append(text[pos:]); path.write_bytes("".join(out).encode())
        else:
            raise ValueError(op)

def timeline(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); calls = {}; ops = []; reviews = []
    for (raw,) in con.execute("select event_json from agent_events order by sequence"):
        p = json.loads(raw)["payload"]
        if p["type"] != "item_finished": continue
        c = p["content"]
        if c["kind"] == "tool_call": calls[c["call_id"]] = c
        elif c["kind"] == "tool_result" and c["outcome"] == "succeeded" and c["call_id"] in calls:
            call = calls[c["call_id"]]
            if call["tool"] == "apply_patch_batch":
                files = call["arguments"]["files"]
                ops.append(json.loads(files) if isinstance(files, str) else files)
            elif call["tool"] == "git_review":
                reviews.append(len(ops))
    return ops, reviews

def snapshot(repo, base, ops):
    with tempfile.TemporaryDirectory(prefix="hx-cf-") as tmp:
        root = pathlib.Path(tmp) / "w"
        subprocess.run([str(GIT), "clone", "-q", "--shared", "--no-checkout", str(cache_for(repo)), str(root)], check=True)
        subprocess.run([str(GIT), "-C", str(root), "checkout", "-q", "--detach", base], check=True)
        for files in ops: apply_ops(root, files)
        return collect_workspace_patch(root, GIT, base, exclude_scratch=True)

results = []
for run, agent in RUNS.items():
    for cdir in sorted((B / "runs" / agent / "cases").glob("case-*")):
        st = json.loads((cdir / "case-state.json").read_text()); iid = st["instance_id"]
        repo = iid.split("__")[0] + "/" + iid.split("__")[1].rsplit("-", 1)[0]
        sealed = cdir / "sealed-workspace.json"
        delivered = json.loads(sealed.read_text())["patch"] if sealed.exists() else ""
        ops, reviews = timeline(cdir / "run/session.sqlite")
        row = {"run": run, "id": iid, "repo": repo, "ops": len(ops), "reviews": reviews}
        try:
            final = snapshot(repo, st["base_commit"], ops)
            row["final_matches_delivered"] = final == delivered
            if reviews:
                row["first_review"] = snapshot(repo, st["base_commit"], ops[: reviews[0]])
                row["last_review"] = snapshot(repo, st["base_commit"], ops[: reviews[-1]])
            row["delivered"] = delivered
        except Exception as error:
            row["error"] = repr(error)[:300]
        results.append(row)
        print(run, iid, row.get("final_matches_delivered"), row.get("error", ""), flush=True)
OUT.write_text(json.dumps(results, ensure_ascii=False))
