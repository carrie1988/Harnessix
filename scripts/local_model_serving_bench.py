"""Measure latency and throughput of a local OpenAI-compatible model server for agent workloads.

Scenarios per prompt size:
  cold      – a prompt with no shared prefix (worst-case first turn);
  warm      – the same prefix already cached, plus a fresh suffix (a typical later agent turn);
  parallel  – N concurrent cold requests (several agents or candidates at once).

Only the standard library is used so the script runs on any inference host.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

TURN_SUFFIX_TOKENS = 2_000


@dataclass(frozen=True)
class RequestResult:
    prompt_tokens: int
    completion_tokens: int
    ttft_s: float
    total_s: float

    @property
    def decode_tokens_per_s(self) -> float:
        decode_time = self.total_s - self.ttft_s
        return (self.completion_tokens - 1) / decode_time if decode_time > 0 else 0.0


class PromptFactory:
    """Builds code-like prompts of a requested token length, calibrated by the server tokenizer."""

    def __init__(self, server_root: str, model: str) -> None:
        self._server_root = server_root
        self._model = model
        sample = self._units(0, 200)
        self._tokens_per_unit = self._count_tokens(sample) / 200

    def build(self, target_tokens: int, seed: str) -> str:
        """A prompt of about `target_tokens` whose first line makes the prefix unique."""
        return f"# session {seed}\n" + self._units(0, self._unit_count(target_tokens))

    def continuation(self, after_tokens: int, extra_tokens: int) -> str:
        """New content that follows a prompt built for `after_tokens`, like the next agent turn."""
        return self._units(self._unit_count(after_tokens), self._unit_count(extra_tokens))

    def _unit_count(self, tokens: int) -> int:
        return max(1, round(tokens / self._tokens_per_unit))

    @staticmethod
    def _units(start: int, count: int) -> str:
        return "".join(
            f"def handle_{i}(request, store):\n"
            f"    record = store.load(request.key_{i % 89})\n"
            f"    if record.version < {i % 13}:\n"
            f"        raise ValueError('stale record {i}')\n"
            f"    return record.apply(request.patch, limit={i % 31})\n\n"
            for i in range(start, start + count)
        )

    def _count_tokens(self, text: str) -> int:
        body = {"model": self._model, "prompt": text}
        return int(_post_json(f"{self._server_root}/tokenize", body)["count"])


def _post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=1_800) as response:
        payload: dict[str, Any] = json.loads(response.read())
    return payload


def stream_completion(api_base: str, model: str, prompt: str, output_tokens: int) -> RequestResult:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a code reviewer."},
            {"role": "user", "content": prompt + "\nSummarize the risks in this module."},
        ],
        "max_tokens": output_tokens,
        "min_tokens": output_tokens,
        "temperature": 0.7,
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
    first_token_at: float | None = None
    usage: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=1_800) as response:
        for raw in response:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            usage = chunk.get("usage") or usage
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            has_text = (
                delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
            )
            if has_text and first_token_at is None:
                first_token_at = time.perf_counter()
    finished = time.perf_counter()
    return RequestResult(
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        ttft_s=(first_token_at or finished) - started,
        total_s=finished - started,
    )


def run_size(
    api_base: str, model: str, prompts: PromptFactory, size: int, output: int, parallel: int
) -> dict[str, Any]:
    cold = stream_completion(api_base, model, prompts.build(size, uuid.uuid4().hex), output)

    prefix = prompts.build(size, uuid.uuid4().hex)
    stream_completion(api_base, model, prefix, 1)  # fill the prefix cache
    warm_prompt = prefix + prompts.continuation(size, TURN_SUFFIX_TOKENS)
    warm = stream_completion(api_base, model, warm_prompt, output)

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [
            pool.submit(
                stream_completion, api_base, model, prompts.build(size, uuid.uuid4().hex), output
            )
            for _ in range(parallel)
        ]
        concurrent = [future.result() for future in futures]
    wall = max(result.total_s for result in concurrent)

    return {
        "prompt_tokens_target": size,
        "cold": asdict(cold) | {"decode_tokens_per_s": cold.decode_tokens_per_s},
        "warm_turn": asdict(warm) | {"decode_tokens_per_s": warm.decode_tokens_per_s},
        "parallel": {
            "requests": parallel,
            "ttft_s_median": statistics.median(r.ttft_s for r in concurrent),
            "ttft_s_max": max(r.ttft_s for r in concurrent),
            "decode_tokens_per_s_median": statistics.median(
                r.decode_tokens_per_s for r in concurrent
            ),
            "aggregate_output_tokens_per_s": sum(r.completion_tokens for r in concurrent) / wall,
            "wall_s": wall,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-base", default="http://127.0.0.1:18000/v1")
    parser.add_argument("--model", default="qwen3.8-flash-next")
    parser.add_argument("--sizes", default="8000,32000,128000", help="prompt token targets")
    parser.add_argument("--output-tokens", type=int, default=512)
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    server_root = args.api_base.removesuffix("/v1")
    prompts = PromptFactory(server_root, args.model)
    results = []
    for size in (int(value) for value in args.sizes.split(",")):
        result = run_size(
            args.api_base, args.model, prompts, size, args.output_tokens, args.parallel
        )
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    report = {
        "api_base": args.api_base,
        "model": args.model,
        "output_tokens": args.output_tokens,
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
