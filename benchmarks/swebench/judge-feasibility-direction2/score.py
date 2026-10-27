import json, sys, collections, pathlib
rows = [json.loads(l) for l in pathlib.Path(sys.argv[1]).read_text().splitlines() if l.strip()]
by = collections.defaultdict(dict)
for r in rows: by[r["instance_id"]]["g" if r["good_first"] else "b"] = r["correct"]
both = sum(1 for v in by.values() if v.get("g") and v.get("b")); none_ = sum(1 for v in by.values() if v.get("g") is False and v.get("b") is False)
incons = len(by) - both - none_
picks = collections.Counter(r["pick"] for r in rows)
print(f"pairs {len(by)} | correct when good=A {sum(1 for v in by.values() if v.get('g'))} | when good=B {sum(1 for v in by.values() if v.get('b'))}"
      f" | consistent-right {both} | consistent-wrong {none_} | order-dependent {incons} | expected selection acc {(both + 0.5*incons)/len(by):.0%}"
      f" | picks {dict(picks)} | cost {rows[-1]['cost_after']} CNY | out tokens avg {sum(r['usage']['completion_tokens'] for r in rows)/len(rows):.0f}")
