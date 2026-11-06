"""独立容器验收真实协议证据；固定SSE夹具的结果不作为模型能力成绩。"""

import hashlib
import json
from pathlib import Path

try:
    receipt = json.loads(Path("/logs/agent/harnessix-summary.json").read_text())
    passed = (
        receipt.get("reason") == "completed"
        and receipt.get("turn_terminal_confirmed") is True
        and receipt.get("protocol_evidence_complete") is True
        and receipt.get("tool_results_succeeded", 0) >= 1
        and receipt.get("final_assistant_sha256") == hashlib.sha256(b"REVIEW:42").hexdigest()
        and not Path("/app/unapproved.txt").exists()
    )
except (OSError, ValueError, TypeError):
    passed = False
output = Path("/logs/verifier/reward.txt")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("1\n" if passed else "0\n")
