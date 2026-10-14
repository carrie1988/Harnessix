"""P0：基于认证轨迹逐消息 Token 计数，标定分词器上界并离线反事实模拟可容纳请求数。

纯离线：只读 extract_wire_tokens.py 产出的数字 JSON，不访问 Session、凭据或网络。
模拟假设轨迹不变（模型不因视图变化而改变行为），只回答“同一轨迹按不同计费方式能走几步”。
用法：python3 replay_budget.py v40-message-tokens.json v27-message-tokens.json replay-summary.json
"""

import json
import statistics
import sys

TASK_TOKENS = 50_000
MAX_REQUESTS = 16
OUTPUT_TOKENS_PER_REQUEST = 130  # 两轮真实输出中位约 110～140
STUB_TOKENS = 40  # 遮蔽后单条工具结果占位（path:行区间 + revision + 摘要）
SLIM_FIXED_TOKENS = 1_500

SCENARIOS = {
    "baseline": dict(fixed=None, keep_tool_steps=None),
    "mask_older_than_2_steps": dict(fixed=None, keep_tool_steps=2),
    "fixed_1500": dict(fixed=SLIM_FIXED_TOKENS, keep_tool_steps=None),
    "fixed_1500_mask_2": dict(fixed=SLIM_FIXED_TOKENS, keep_tool_steps=2),
    "fixed_1500_mask_1": dict(fixed=SLIM_FIXED_TOKENS, keep_tool_steps=1),
}


def completed_attempts(case):
    return [
        a for a in case["attempts"]
        if a["input_tokens"] and a["status"].endswith("completed")
        and a["completeness"].endswith("complete")
    ]


def fixed_tokens(case):
    first = next(a for a in completed_attempts(case) if a["step"] == 1)
    step_one = next(s for s in case["steps"] if s["step"] == 1)
    return first["input_tokens"] - step_one["history_tokens"]


def calibration_points(case):
    if case.get("compactions"):
        return []
    steps = {s["step"]: s for s in case["steps"]}
    fixed = fixed_tokens(case)
    return [
        a["input_tokens"] - (fixed + steps[a["step"]]["history_tokens"])
        for a in completed_attempts(case)
        if a["step"] > 1 and steps.get(a["step"], {}).get("append_only")
    ]


def affordable_requests(case, fixed, keep_tool_steps):
    blocks = {}
    for message in case["messages"]:
        blocks.setdefault(message["first_step"], []).append(message)
    first_step = min(blocks)
    later = [b for s, b in blocks.items() if s > first_step]
    mean_tool = statistics.mean(sum(m["tokens"] for m in b if m["role"] == "tool") for b in later)
    mean_assistant = statistics.mean(
        sum(m["tokens"] for m in b if m["role"] == "assistant") for b in later
    )
    prompt = sum(m["tokens"] for m in blocks[first_step])
    history, used, requests = [], 0, 0
    for step in range(1, MAX_REQUESTS + 1):
        if step > 1:
            if step in blocks:
                history += [(step, m["role"], m["tokens"]) for m in blocks[step]]
            else:  # 观测轨迹之外按该 Case 平均每步新增量外推
                history += [(step, "assistant", mean_assistant), (step, "tool", mean_tool)]
        masked = keep_tool_steps is not None
        history_tokens = sum(
            STUB_TOKENS if masked and role == "tool" and origin <= step - keep_tool_steps else tokens
            for origin, role, tokens in history
        )
        request = fixed + prompt + history_tokens
        if used + request + OUTPUT_TOKENS_PER_REQUEST > TASK_TOKENS:
            break
        used += request + OUTPUT_TOKENS_PER_REQUEST
        requests += 1
    return requests


def case_row(case, source):
    fixed = fixed_tokens(case)
    row = {
        "source": source,
        "arm": case["arm"] or None,
        "index": case["index"],
        "instance_id": case["instance_id"],
        "append_only": all(s["append_only"] for s in case["steps"]),
        "observed_completed_requests": len(completed_attempts(case)),
        "fixed_tokens": fixed,
    }
    for name, scenario in SCENARIOS.items():
        row[name] = affordable_requests(
            case, scenario["fixed"] or fixed, scenario["keep_tool_steps"]
        )
    return row


def distribution(values):
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def main():
    v40_path, v27_path, output = sys.argv[1:4]
    sources = {"v40": json.load(open(v40_path)), "v27": json.load(open(v27_path))}
    rows = [case_row(c, name) for name, cases in sources.items() for c in cases]
    errors = [e for cases in sources.values() for c in cases for e in calibration_points(c)]
    clean = [r for r in rows if r["append_only"]]
    summary = {
        "schema": "harnessix.swebench-budget-replay-p0/v1",
        "scope": "已认证开发集轨迹的只读离线反事实；不是新Agent轨迹、实测收益或官方成绩",
        "model_requests": 0,
        "tokenizer": {
            "source": "huggingface.co/Qwen/Qwen3-8B tokenizer.json",
            "sha256": "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
            "counted_unit": "每条出站 history 消息的 JSON 编码",
        },
        "calibration": {
            "points": len(errors),
            "underestimates": sum(e > 0 for e in errors),
            "actual_minus_predicted_tokens": distribution(errors),
        },
        "fixed_tokens": distribution([r["fixed_tokens"] for r in rows]),
        "assumptions": {
            "task_tokens": TASK_TOKENS,
            "output_tokens_per_request": OUTPUT_TOKENS_PER_REQUEST,
            "stub_tokens": STUB_TOKENS,
            "slim_fixed_tokens": SLIM_FIXED_TOKENS,
            "trajectory_unchanged": True,
        },
        "baseline_reproduces_observed_in_append_only_cases": sum(
            r["baseline"] == r["observed_completed_requests"] for r in clean
        ),
        "append_only_cases": len(clean),
        "affordable_requests": {
            scope: {name: distribution([r[name] for r in subset]) for name in SCENARIOS}
            for scope, subset in (("all", rows), ("append_only", clean))
        },
        "cases": rows,
    }
    with open(output, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps({k: summary[k] for k in (
        "calibration", "fixed_tokens", "baseline_reproduces_observed_in_append_only_cases",
        "append_only_cases", "affordable_requests")}, ensure_ascii=False, indent=1))


main()
