"""Probe how reliably a local OpenAI-compatible model server returns well-typed tool calls.

Each probe runs several times; a run passes only when the HTTP call succeeds and every tool
call parses as JSON and matches the declared parameter types (arrays must not arrive as strings).
Only the standard library is used so the script runs on any inference host.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a line range of a workspace file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_files",
            "description": "Apply exact text replacements to one or more files in one transaction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "edits": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "old": {"type": "string"},
                                            "new": {"type": "string"},
                                        },
                                        "required": ["old", "new"],
                                    },
                                },
                            },
                            "required": ["path", "edits"],
                        },
                    },
                    "dry_run": {"type": "boolean"},
                },
                "required": ["files", "dry_run"],
            },
        },
    },
]
SCHEMAS = {tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOLS}
SYSTEM = "You are a coding agent. Use the provided tools; never describe a tool call in prose."
EDIT_REQUEST = (
    "Both files were already read; current contents:\n"
    "--- src/a.py ---\nx = 0\n\ndef add(value):\n    global x\n    x += value\n    return x\n"
    "--- src/b.py ---\nfrom a import x\n\nprint(x)\n"
    "Rename the variable x to total in both files (every occurrence). Do not read the files again; "
    "apply all replacements with a single edit_files call, not a dry run."
)
PRIOR_EDIT_ARGUMENTS = {
    "files": [{"path": "src/c.py", "edits": [{"old": "y = 1", "new": "count = 1"}]}],
    "dry_run": True,
}


@dataclass
class ProbeOutcome:
    """`passed` = expected tool with well-typed arguments; call counts measure typing alone."""

    runs: int = 0
    passed: int = 0
    calls: int = 0
    well_typed_calls: int = 0
    failures: Counter[str] = field(default_factory=Counter)


def type_errors(value: Any, schema: dict[str, Any], where: str = "$") -> list[str]:
    """Return mismatches between a value and a minimal JSON schema.

    Supported keywords: type, properties, items, required.
    """
    expected = schema.get("type")
    checks: dict[str, Callable[[Any], bool]] = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
    }
    if expected in checks and not checks[expected](value):
        return [f"{where}: expected {expected}, got {type(value).__name__}"]
    errors: list[str] = []
    if expected == "object":
        errors += [
            f"{where}.{name}: missing" for name in schema.get("required", []) if name not in value
        ]
        for name, child in schema.get("properties", {}).items():
            if name in value:
                errors += type_errors(value[name], child, f"{where}.{name}")
    if expected == "array":
        for index, item in enumerate(value):
            errors += type_errors(item, schema.get("items", {}), f"{where}[{index}]")
    return errors


def chat(
    api_base: str, model: str, messages: list[dict[str, Any]], thinking: bool
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.7,
        "max_tokens": 4096 if thinking else 1024,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    request = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        payload: dict[str, Any] = json.loads(response.read())
    return payload


def argument_error(call: dict[str, Any]) -> str | None:
    """Return why a tool call's arguments are malformed, or None when they parse and type-check."""
    name = call["function"]["name"]
    if name not in SCHEMAS:
        return f"unknown tool {name}"
    try:
        arguments = json.loads(call["function"]["arguments"])
    except json.JSONDecodeError:
        return "arguments are not valid JSON"
    errors = type_errors(arguments, SCHEMAS[name])
    return "type mismatch: " + errors[0] if errors else None


def check_calls(
    message: dict[str, Any], expected_name: str, minimum: int, outcome: ProbeOutcome
) -> str | None:
    """Count argument typing for every call; return why the run fails, or None when it passes."""
    calls = message.get("tool_calls") or []
    malformed = [error for error in map(argument_error, calls) if error]
    outcome.calls += len(calls)
    outcome.well_typed_calls += len(calls) - len(malformed)
    if malformed:
        return malformed[0]
    if len(calls) < minimum:
        return f"expected >= {minimum} tool calls, got {len(calls)}"
    unexpected = [
        call["function"]["name"] for call in calls if call["function"]["name"] != expected_name
    ]
    return f"chose another tool: {unexpected[0]}" if unexpected else None


def history_with_prior_edit() -> list[dict[str, Any]]:
    """A conversation where an earlier edit_files call (array arguments) is already in history."""
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Rename y to count in src/c.py, dry run first."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_prior_1",
                    "type": "function",
                    "function": {
                        "name": "edit_files",
                        "arguments": json.dumps(PRIOR_EDIT_ARGUMENTS),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_prior_1",
            "content": "dry run ok: 1 replacement in src/c.py",
        },
        {"role": "user", "content": EDIT_REQUEST},
    ]


PROBES: dict[str, tuple[Callable[[], list[dict[str, Any]]], str, int, bool]] = {
    "nested_arguments": (
        lambda: [{"role": "system", "content": SYSTEM}, {"role": "user", "content": EDIT_REQUEST}],
        "edit_files",
        1,
        False,
    ),
    "history_with_array_arguments": (history_with_prior_edit, "edit_files", 1, False),
    "parallel_reads": (
        lambda: [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "Read lines 1-40 of src/a.py and lines 1-40 of src/b.py. "
                    "Call both reads now in parallel."
                ),
            },
        ],
        "read_file",
        2,
        False,
    ),
    "thinking_nested_arguments": (
        lambda: [{"role": "system", "content": SYSTEM}, {"role": "user", "content": EDIT_REQUEST}],
        "edit_files",
        1,
        True,
    ),
    "thinking_history_with_array_arguments": (history_with_prior_edit, "edit_files", 1, True),
}


def run_probe(api_base: str, model: str, name: str, runs: int) -> ProbeOutcome:
    build_messages, expected_name, minimum, thinking = PROBES[name]
    outcome = ProbeOutcome()
    for _ in range(runs):
        outcome.runs += 1
        try:
            response = chat(api_base, model, build_messages(), thinking)
        except urllib.error.HTTPError as error:
            outcome.failures[
                f"HTTP {error.code}: {error.read()[:160].decode(errors='replace')}"
            ] += 1
            continue
        reason = check_calls(response["choices"][0]["message"], expected_name, minimum, outcome)
        if reason is None:
            outcome.passed += 1
        else:
            outcome.failures[reason] += 1
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-base", default="http://127.0.0.1:18000/v1")
    parser.add_argument("--model", default="qwen3.8-flash-next")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    report = {}
    for name in PROBES:
        outcome = run_probe(args.api_base, args.model, name, args.runs)
        report[name] = {
            "runs": outcome.runs,
            "passed": outcome.passed,
            "calls": outcome.calls,
            "well_typed_calls": outcome.well_typed_calls,
            "failures": dict(outcome.failures),
        }
        print(name, json.dumps(report[name], ensure_ascii=False), flush=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
