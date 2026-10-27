"""Direction-2 selector feasibility: can deepseek-v4-pro pick the resolved patch from a decisive pair
using only the issue text and the two diffs? Official labels are used only to score the judge.
Every request goes through the local Stash proxy; the API key is read into memory only."""
import json, os, pathlib, re, sys, fcntl
from datetime import datetime, UTC
import httpx

HERE = pathlib.Path(__file__).parent
BASE = HERE.parent
LEDGER = BASE / "budget-period-20261026-60-cny.json"; PERIOD = "7fa6f68f-95ad-499e-844e-1066104ef4ba"
MODE = sys.argv[1]; TAG = sys.argv[2]; MAX_CNY = float(sys.argv[3])
PRICE = {"miss": 9.0, "hit": 0.30, "out": 27.0}  # CNY per million tokens (deepseek-v4-pro catalog)
SYSTEM = ("You review candidate patches for a GitHub issue in a Python repository. Maintainers' hidden tests "
          "will check that the issue is fixed exactly as described and that existing behaviour is unchanged.")

def clip(text, limit=8000):
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"

def prompt(issue, first, second):
    return (f"Issue:\n{clip(issue)}\n\nPatch A:\n```diff\n{clip(first)}\n```\n\nPatch B:\n```diff\n{clip(second)}\n```\n\n"
            "Exactly one patch resolves the issue. Determine what the issue requires (including edge cases and every "
            "code path it mentions), check each patch against it, then end with one line `ANSWER: A` or `ANSWER: B`.")

def ledger(update):
    with open(BASE / ".budget-period.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(LEDGER.read_text()); assert data["period_id"] == PERIOD
        update(data); tmp = LEDGER.with_suffix(".tmp"); tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n"); os.replace(tmp, LEDGER)

def used(d):
    return sum(float(a.get("known_cost_amount", 0)) + float(a.get("reserved_cost_amount", 0)) if a["status"].startswith("settled_")
               else float(a["maximum_amount"]) for a in d["allocations"])

def main():
    pairs = json.loads((HERE / "pairs.json").read_text()); run_dir = f"judge-feasibility-{TAG}"
    def reserve(d):
        assert used(d) + MAX_CNY <= float(d["limit_amount"]) and not any(a["run_directory"] == run_dir for a in d["allocations"])
        d["allocations"].append({"plan_sha256": "judge-feasibility-direction-2", "execution_fingerprint": run_dir, "run_directory": run_dir,
                                 "maximum_amount": str(MAX_CNY), "status": "reserved_execution", "reserved_at": datetime.now(UTC).isoformat()})
    ledger(reserve)
    out = HERE / f"results-{TAG}.jsonl"; cost = 0.0
    key = (BASE / "deepseek" / "api-key").read_text().strip()
    try:
        with httpx.Client(trust_env=False, timeout=600, proxy="http://127.0.0.1:7890") as client, open(out, "a") as log:
            for pair in pairs:
                for good_first in (True, False):
                    if cost > MAX_CNY - 0.3: raise SystemExit("cost cap reached")
                    a, b = (pair["good"], pair["bad"]) if good_first else (pair["bad"], pair["good"])
                    options = ({"temperature": 0, "max_tokens": 2048, "thinking": {"type": "disabled"}} if MODE == "plain"
                               else {"max_tokens": 16384, "thinking": {"type": "enabled"}})
                    body = {"model": "deepseek-v4-pro", **options, "messages": [
                        {"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt(pair["issue"], a["patch"], b["patch"])}]}
                    r = client.post("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
                    r.raise_for_status(); j = r.json(); u = j["usage"]
                    hit = u.get("prompt_cache_hit_tokens", 0); miss = u.get("prompt_cache_miss_tokens", u["prompt_tokens"] - hit)
                    cost += (miss * PRICE["miss"] + hit * PRICE["hit"] + u["completion_tokens"] * PRICE["out"]) / 1e6
                    text = j["choices"][0]["message"].get("content") or ""
                    picks = re.findall(r"ANSWER:\s*\**\s*([AB])", text); pick = picks[-1] if picks else None
                    row = {"instance_id": pair["instance_id"], "good_first": good_first, "pick": pick,
                           "correct": None if pick is None else (pick == "A") == good_first,
                           "good_source": pair["good"]["source"], "bad_source": pair["bad"]["source"], "usage": u, "cost_after": round(cost, 4)}
                    log.write(json.dumps(row, ensure_ascii=False) + "\n"); log.flush()
                    print(pair["instance_id"], "good_first" if good_first else "bad_first", pick, row["correct"], round(cost, 3), flush=True)
    finally:
        key = None
        def settle(d):
            m = [a for a in d["allocations"] if a["run_directory"] == run_dir and a["status"] == "reserved_execution"]
            assert len(m) == 1; m[0].update(status="settled_execution_completed", known_cost_amount=f"{cost:.4f}", reserved_cost_amount="0", settled_at=datetime.now(UTC).isoformat())
        ledger(settle)

if __name__ == "__main__":
    main()
