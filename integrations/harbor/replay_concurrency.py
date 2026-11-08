"""在不同并发下重放同一批真实智能体模型调用，测量单次调用的首字时间、总耗时与吞吐。

输入是mini-swe-agent在Harbor试次中留下的轨迹（`agent/mini-swe-agent.trajectory.json`），其中保存了
每次调用发给模型的完整消息。每个会话按原顺序逐步重放：第k次调用的消息是轨迹中第k条助手回复之前
的全部消息，工具定义与请求参数与mini的R组配置一致；输出长度固定为原调用记录的完成Token数
（`ignore_eos`），使各档并发的解码工作量相同。

每档并发开头在系统消息前加一个随机标记，各档之间不能复用彼此的前缀缓存，同一会话内的逐步调用
仍能复用。N个工作线程从同一个会话队列取会话，每个线程把取到的会话从头到尾重放完再取下一个；
会话按序号交替分到各个目标端点，主机上的转发按来访地址固定分配，因此直接访问各副本。

只依赖标准库，可在推理主机上直接运行；不记录消息正文，只输出计时与Token计数。
"""

from __future__ import annotations

import argparse
import json
import queue
import statistics
import threading
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute"}
            },
            "required": ["command"],
        },
    },
}
_MESSAGE_FIELDS = ("role", "content", "tool_calls", "tool_call_id", "name")


@dataclass(frozen=True)
class Call:
    messages: tuple[dict[str, Any], ...]
    completion_tokens: int


@dataclass(frozen=True)
class CallResult:
    level: int
    session: int
    step: int
    endpoint: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None
    ttft_s: float
    total_s: float
    error: str | None


def _api_message(message: dict[str, Any]) -> dict[str, Any]:
    """mini发往模型前去掉extra；正文总是发送（空回复为空字符串），其余空的可选字段不发送。"""
    kept = {k: message[k] for k in _MESSAGE_FIELDS if message.get(k) is not None}
    return {**kept, "content": message.get("content") or ""}


def session_calls(trajectory: dict[str, Any], max_steps: int) -> list[Call]:
    """按原顺序取出一个会话的前max_steps次模型调用；没有用量记录的回复不重放。"""
    messages = trajectory["messages"]
    calls: list[Call] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        usage = ((message.get("extra") or {}).get("response") or {}).get("usage") or {}
        completion = usage.get("completion_tokens")
        if not isinstance(completion, int) or completion < 1:
            continue
        calls.append(Call(tuple(_api_message(m) for m in messages[:index]), completion))
        if len(calls) == max_steps:
            break
    return calls


def with_nonce(messages: tuple[dict[str, Any], ...], nonce: str) -> list[dict[str, Any]]:
    """在第一条消息正文前加本档并发的标记，使各档之间不共享前缀。"""
    first = dict(messages[0])
    first["content"] = f"[replay {nonce}]\n{first.get('content') or ''}"
    return [first, *messages[1:]]


def stream_call(
    api_base: str, model: str, messages: list[dict[str, Any]], tokens: int
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        "tools": [BASH_TOOL],
        "parallel_tool_calls": True,
        "max_tokens": tokens,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    first: float | None = None
    usage: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=1800) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[5:])
            if first is None and chunk.get("choices"):
                first = time.perf_counter()
            if chunk.get("usage"):
                usage = chunk["usage"]
    finished = time.perf_counter()
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": details.get("cached_tokens"),
        "ttft_s": (first or finished) - started,
        "total_s": finished - started,
    }


def run_level(
    level: int, sessions: list[list[Call]], endpoints: list[str], model: str
) -> list[CallResult]:
    nonce = uuid.uuid4().hex
    pending: queue.Queue[int] = queue.Queue()
    for index in range(len(sessions)):
        pending.put(index)
    results: list[CallResult] = []
    lock = threading.Lock()

    def worker() -> None:
        while True:
            try:
                index = pending.get_nowait()
            except queue.Empty:
                return
            endpoint = endpoints[index % len(endpoints)]
            for step, call in enumerate(sessions[index]):
                try:
                    measured = stream_call(
                        endpoint, model, with_nonce(call.messages, nonce), call.completion_tokens
                    )
                    error = None
                except Exception as failure:  # 计时工具：记录失败原因并继续下一次调用
                    measured = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cached_tokens": None,
                        "ttft_s": 0.0,
                        "total_s": 0.0,
                    }
                    error = type(failure).__name__
                with lock:
                    results.append(
                        CallResult(level, index, step, endpoint, error=error, **measured)
                    )

    threads = [threading.Thread(target=worker) for _ in range(level)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else 0.0


def summarize(level: int, results: list[CallResult], wall_s: float) -> dict[str, Any]:
    ok = [r for r in results if r.error is None]
    ttft, total = [r.ttft_s for r in ok], [r.total_s for r in ok]
    return {
        "level": level,
        "calls": len(results),
        "errors": len(results) - len(ok),
        "wall_s": round(wall_s, 1),
        "calls_per_min": round(60 * len(ok) / wall_s, 2) if wall_s else 0.0,
        "output_tokens_per_s": round(sum(r.completion_tokens for r in ok) / wall_s, 1)
        if wall_s
        else 0.0,
        "ttft_p50_s": round(_quantile(ttft, 0.5), 2),
        "ttft_p90_s": round(_quantile(ttft, 0.9), 2),
        "total_p50_s": round(_quantile(total, 0.5), 2),
        "total_p90_s": round(_quantile(total, 0.9), 2),
        "total_mean_s": round(statistics.fmean(total), 2) if total else 0.0,
        "prompt_tokens_mean": round(statistics.fmean([r.prompt_tokens for r in ok]), 0)
        if ok
        else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trajectories", type=Path, nargs="+", required=True)
    parser.add_argument("--endpoints", required=True, help="以逗号分隔的API根地址")
    parser.add_argument("--model", default="qwen3.8-flash-next")
    parser.add_argument("--levels", default="4,8,16")
    parser.add_argument("--sessions", type=int, default=16)
    parser.add_argument("--steps", type=int, default=20, help="每个会话重放的前若干次调用")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sessions = [
        calls
        for path in sorted(args.trajectories)
        if len(calls := session_calls(json.loads(path.read_text()), args.steps)) == args.steps
    ][: args.sessions]
    if len(sessions) < args.sessions:
        raise SystemExit(f"只有{len(sessions)}个会话达到{args.steps}次调用")
    endpoints = [e.strip().rstrip("/") for e in args.endpoints.split(",")]
    report: dict[str, Any] = {"sessions": len(sessions), "steps": args.steps, "levels": []}
    records: list[dict[str, Any]] = []
    for level in (int(v) for v in args.levels.split(",")):
        started = time.perf_counter()
        results = run_level(level, sessions, endpoints, args.model)
        summary = summarize(level, results, time.perf_counter() - started)
        report["levels"].append(summary)
        records.extend(asdict(r) for r in results)
        print(json.dumps(summary), flush=True)
    args.output.write_text(json.dumps({**report, "calls": records}, indent=1))


if __name__ == "__main__":
    main()
