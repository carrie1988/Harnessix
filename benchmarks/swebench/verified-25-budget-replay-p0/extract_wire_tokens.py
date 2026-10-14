"""P0：认证只读重建历史出站消息并用 Qwen 分词器计数；只输出数字，不保存正文。

用法：在开发工作树内以 PYTHONPATH=src 运行；argv[1]=run 根目录，argv[2]=arm 列表(逗号)，
argv[3]=case 数，argv[4]=输出 JSON，argv[5]=tokenizer.json，argv[6]=tokenizers site-packages。
"""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

RUN_ROOT, ARMS, CASES, OUTPUT, TOKENIZER, TOKENIZER_SITE = sys.argv[1:7]
sys.path.append(TOKENIZER_SITE)
from tokenizers import Tokenizer  # noqa: E402

from harnessix.agent.cancellation import CancelToken  # noqa: E402
from harnessix.agent.reducer import replay  # noqa: E402
from harnessix.context.compaction_window import prepare_active_model_history  # noqa: E402
from harnessix.context.tool_result_contracts import ToolResultViewPolicy  # noqa: E402
from harnessix.evals.swebench.execution_io import read_case_execution_state  # noqa: E402
from harnessix.evals.swebench.io import read_preregistration_plan  # noqa: E402
from harnessix.evals.swebench.run_config_io import read_swebench_agent_run_config  # noqa: E402
from harnessix.evals.task_pack_publication import (  # noqa: E402
    TaskPackHistoryReadControl,
    open_task_pack_publication,
    provider_publication_scope,
)
from harnessix.models._chat_mapping import build_request  # noqa: E402
from harnessix.models._history import encode_json  # noqa: E402
from harnessix.models.contracts import ModelRequest  # noqa: E402

KEYCHAIN = ["/usr/bin/security", "find-generic-password", "-s",
            "TTPs_Agentic_GraphRAG_Bailian_API_Key", "-a", "ttp-graphrag", "-w"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def history_messages(turn, thread, step, history, config):
    request = ModelRequest(
        thread_id=thread.thread_id, turn_id=turn.turn_id, step=step, history=history,
        tools=(), instructions=None, budget=turn.budget,
    )
    body, _ = build_request(request, config, include_catalog_note=False)
    return body["messages"]


def message_row(message, tokenizer):
    encoded = encode_json(message)
    calls = [c["function"]["name"] for c in message.get("tool_calls") or ()]
    return {
        "role": message["role"],
        "bytes": len(encoded.encode()),
        "tokens": len(tokenizer.encode(encoded, add_special_tokens=False).ids),
        "tool_calls": calls,
    }


async def read_case(root, plan, config, key, tokenizer):
    files = (root / "case-state.json", root / "run/session.sqlite")
    before = [sha(f) for f in files]
    state = read_case_execution_state(files[0], plan)
    control = TaskPackHistoryReadControl.begin(CancelToken())
    with provider_publication_scope(config.provider_config.api_key_env, key) as scope:
        async with open_task_pack_publication(
            root / "run", scope, existing_only=True, history_read=control
        ) as owner:
            carrier = await owner.authenticated_thread_history(state.thread_id, control)
    thread, events = carrier.thread, carrier.events
    final = thread.turns[0]
    steps, messages, view_mismatch = [], [], 0
    for n, event in enumerate(events):
        if event.payload.type != "model_history_prepared":
            continue
        prefix = replay(events[: n + 1])
        turn = prefix.turns[0]
        step = event.payload.inspection.model_step
        prepared = prepare_active_model_history(prefix, step, ToolResultViewPolicy())
        if prepared.inspection.view_history_sha256 != event.payload.inspection.view_history_sha256:
            view_mismatch += 1
        wire = history_messages(turn, thread, step, prepared.history, config.provider_config)
        rows = [message_row(m, tokenizer) for m in wire]
        # 历史只追加（无压缩时）：前缀一致则记录新增消息首次出现的步号。
        prefix_ok = len(rows) >= len(messages) and all(
            a["bytes"] == b["bytes"] for a, b in zip(messages, rows)
        )
        for row in rows[len(messages):] if prefix_ok else rows:
            row["first_step"] = step
        messages = messages + rows[len(messages):] if prefix_ok else rows
        steps.append({
            "step": step,
            "history_messages": len(rows),
            "history_tokens": sum(r["tokens"] for r in rows),
            "history_bytes": sum(r["bytes"] for r in rows),
            "append_only": prefix_ok,
            "remaining_task_tokens": turn.budget.max_tokens - turn.usage.total_tokens,
        })
    attempts = [
        {"step": a.step, "status": str(a.status), "input_tokens": a.usage.input_tokens,
         "output_tokens": a.usage.output_tokens, "completeness": str(a.usage.completeness)}
        for a in final.model_attempts
    ]
    assert before == [sha(f) for f in files], "Native 或 Case 状态被改变"
    return {
        "instance_id": state.instance_id if hasattr(state, "instance_id") else None,
        "status": str(final.status),
        "native_sha256": before[1],
        "view_mismatch": view_mismatch,
        "compactions": len(final.compactions) if hasattr(final, "compactions") else None,
        "steps": steps,
        "messages": messages,
        "attempts": attempts,
    }


async def main():
    tokenizer = Tokenizer.from_file(TOKENIZER)
    key = subprocess.run(KEYCHAIN, capture_output=True, check=True, timeout=30).stdout.decode().strip()
    out = []
    try:
        for arm in ARMS.split(","):
            arm_root = Path(RUN_ROOT) / arm if arm else Path(RUN_ROOT)
            plan = read_preregistration_plan(arm_root / "plan.json") if (arm_root / "plan.json").exists() else None
            config = read_swebench_agent_run_config(arm_root / "run-config.json")
            if plan is None:
                plan = read_preregistration_plan(Path(sys.argv[7]))
            for index in range(int(CASES)):
                root = arm_root / "cases" / f"case-{index:03d}"
                row = await read_case(root, plan, config, key, tokenizer)
                row.update(arm=arm, index=index, instance_id=plan.cases[index].instance_id)
                out.append(row)
                print(arm, index, row["status"], "steps", len(row["steps"]), "view_mismatch", row["view_mismatch"], flush=True)
    finally:
        key = None
    Path(OUTPUT).write_text(json.dumps(out, ensure_ascii=False, indent=1))


asyncio.run(main())
