"""Write the preregistration of one Bailian run with plan, run-config and host hashes (run from the source root).

Usage: preregister-bailian.py <tag> <public plan directory> <schema kind: development|generalization> <json fields>"""
import hashlib
import json
import sys
from datetime import UTC, datetime

B = "/Users/zhangjinhui/Library/Application Support/Harnessix/swebench/"
TAG, PUBLIC, KIND, FIELDS = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
assert KIND in {"development", "generalization"}


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


plan = json.load(open(PUBLIC + "/plan.json"))
root = B + "runs/agent-" + plan["plan_sha256"][:12] + "-" + TAG + "/"
config = json.load(open(root + "run-config.json"))
record = {
    "schema": f"harnessix.swebench-{KIND}-preregistration/v1",
    "frozen_at": datetime.now(UTC).isoformat(),
    "plan_sha256": plan["plan_sha256"],
    "plan_file_sha256": digest(PUBLIC + "/plan.json"),
    "source_revision": plan["harnessix_revision"],
    "model": "deepseek-v4-pro-0813 on Aliyun Bailian (DeepSeek official deepseek-v4-pro is the same version), via local proxy 127.0.0.1:7890",
    "run_config_spec": config["spec_version"],
    "run_config_file_sha256": digest(root + "run-config.json"),
    "fee_stop_amount": config["fee_stop_amount"],
    "price": "Bailian Beijing idle prices (miss 4.5, implicit-cache hit 0.9, output 13.5 CNY per million); every case starts only inside the 22:00-08:00 Beijing window with its whole 2700 s budget + 10 min left",
    "prepare_host_sha256": digest(B + "prepare-bailian.py"),
    "execute_host_sha256": digest(B + "execute-bailian.py"),
    "evaluate_host_sha256": digest(B + "evaluate-bailian.py"),
    "task_budget": "{max_tokens} tokens / {max_steps} steps / {timeout_seconds:g} s / {max_tool_calls_per_step} tool calls per step".format(**plan["task_budget"]),
    "budget_period_id": "d7a14312-1a8e-445a-8adb-2e171e336dcb",
    "attempt_policy": "single attempt per case; no retry, no best-of-N, no stitching",
    **FIELDS,
}
with open(PUBLIC + "/preregistration.json", "w") as handle:
    json.dump(record, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
print(json.dumps({"preregistered": TAG, "plan": plan["plan_sha256"][:12], "spec": config["spec_version"]}))
