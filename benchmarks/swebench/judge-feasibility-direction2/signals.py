"""Resolution rate by first-trajectory signals across v57p, v61r, v62 (read-only)."""
import json, sqlite3, pathlib, glob, collections
B = pathlib.Path("/Users/zhangjinhui/Library/Application Support/Harnessix/swebench/runs")
RUNS = {"v57p": ("agent-9059bcbc2209-v57p", "official-9059bcbc2209-v57p"), "v61r": ("agent-6c2036bce49c-v61r", "official-6c2036bce49c-v61r"), "v62": ("agent-fbc53720e7af-v62", "official-fbc53720e7af-v62")}
rows = []
for run, (agent, off) in RUNS.items():
    ok = set(json.load(open(glob.glob(str(B / off / "logs/evaluation/*/results.json"))[0]))["resolved_ids"])
    for cdir in sorted((B / agent / "cases").glob("case-*")):
        st = json.loads((cdir / "case-state.json").read_text()); con = sqlite3.connect(f"file:{cdir/'run/session.sqlite'}?mode=ro", uri=True)
        calls = {}; tests = []; patches = 0; reviews = 0
        for (raw,) in con.execute("select event_json from agent_events order by sequence"):
            p = json.loads(raw)["payload"]
            if p["type"] != "item_finished": continue
            c = p["content"]
            if c["kind"] == "tool_call": calls[c["call_id"]] = c["tool"]
            elif c["kind"] == "tool_result":
                t = calls.get(c["call_id"], "")
                if t.startswith("run_profile"): tests.append(c["outcome"] == "succeeded")
                elif t == "apply_patch_batch" and c["outcome"] == "succeeded": patches += 1
                elif t == "git_review" and c["outcome"] == "succeeded": reviews += 1
        err = st.get("error_code")
        clean = err is None and tests and tests[-1] and patches <= 3 and tests.count(False) <= 1
        sig = "clean_closure" if clean else ("normal_but_struggled" if err is None or err == "swebench_public_tests_failed" else "abnormal_end")
        rows.append((sig, st["instance_id"] in ok, st["input_tokens"]))
c = collections.defaultdict(lambda: [0, 0, 0])
for s, r, t in rows: c[s][0] += 1; c[s][1] += r; c[s][2] += t
for s, (n, r, t) in c.items(): print(f"{s:22} n={n:2} resolved {r}/{n} ({100*r/n:.0f}%) avg_tokens {t/n/1e6:.2f}M")
