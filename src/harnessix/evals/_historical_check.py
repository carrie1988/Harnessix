"""在隔离子进程中运行内置历史任务检查；只输出固定诊断。"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any, cast


class CheckFailed(Exception):
    pass


def _prepare_workspace(value: str) -> None:
    root = Path(value).resolve(strict=True)
    source = root / "src"
    if not source.is_dir() or not (root / "tests" / "models" / "wire.py").is_file():
        raise RuntimeError
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(source))
    os.environ["HARNESSIX_TEST_KEY"] = "historical-eval-fixture"
    os.environ.pop("OPENAI_CUSTOM_HEADERS", None)


def _empty_id_frames(arguments: str | None) -> list[bytes]:
    wire_helpers = importlib.import_module("tests.models.wire")

    parts = wire_helpers.tool_frames("{}" if arguments is None else "{")
    parts.insert(
        1,
        wire_helpers.frame(
            wire_helpers.chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "",
                            "type": "function",
                            "function": {"arguments": arguments},
                        }
                    ]
                }
            )
        ),
    )
    return cast(list[bytes], parts)


async def _collect(parts: list[bytes]) -> tuple[list[Any], Any]:
    tests = importlib.import_module("tests.models.test_openai_chat")
    return cast(tuple[list[Any], Any], await tests.collect(parts))


async def _empty_id_behavior() -> None:
    from harnessix.models.contracts import ResponseCompleted, ToolCallCompleted

    wire_helpers = importlib.import_module("tests.models.wire")

    for arguments in (None, "}"):
        events, wire = await _collect(_empty_id_frames(arguments))
        calls = [event for event in events if isinstance(event, ToolCallCompleted)]
        if not (
            len(calls) == 1
            and calls[0].call_id == wire_helpers.call()["id"]
            and calls[0].arguments == {}
            and isinstance(events[-1], ResponseCompleted)
            and events[-1].usage.total_tokens == 12
            and wire.closed
        ):
            raise CheckFailed


async def _identity_guards() -> None:
    from harnessix.models.contracts import ResponseCompleted, ResponseFailed, ToolCallCompleted

    wire_helpers = importlib.import_module("tests.models.wire")

    for violation in ("no_identity", "id_drift", "name_drift", "invalid_type"):
        parts = _empty_id_frames(None)
        if violation == "no_identity":
            first = wire_helpers.call()
            first["id"] = ""
            parts[0] = wire_helpers.frame(wire_helpers.chunk({"tool_calls": [first]}))
        else:
            update: dict[str, Any] = {"index": 0}
            if violation == "id_drift":
                update["id"] = "another-id"
            elif violation == "name_drift":
                update["function"] = {"name": ""}
            else:
                update["type"] = ""
            parts.insert(2, wire_helpers.frame(wire_helpers.chunk({"tool_calls": [update]})))
        events, stream = await _collect(parts)
        if not (
            events[-1] == ResponseFailed(code="invalid_provider_output")
            and not any(
                isinstance(event, ToolCallCompleted | ResponseCompleted) for event in events
            )
            and stream.closed
        ):
            raise CheckFailed


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        _prepare_workspace(sys.argv[1])
        checks = {
            "empty_id_behavior": _empty_id_behavior,
            "identity_guards": _identity_guards,
        }
        check = checks.get(sys.argv[2])
        if check is None:
            return 2
        asyncio.run(check())
    except CheckFailed:
        print("historical-check: failed")
        return 1
    except BaseException:
        print("historical-check: infrastructure-error")
        return 2
    print("historical-check: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
