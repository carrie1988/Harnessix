"""公开保护检查做不完时只让那一次调用失败，本轮与同批调用照常继续（ADR 0174，Beta Issue #37）。

0.10.0b55：搜索的完整存档或结果本身超过检查上限时，整轮以public_output_limit失败，同批后面的调用不再记录。
发现Secret仍让整轮失败。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolResultContent, Turn, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_result_protection import PROTECTION_LIMIT_FEEDBACK
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.models.tool_result_text import tool_result_text
from harnessix.secrets import publication
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.agent.test_publication import CANARY, REFERENCE, protected
from tests.artifacts.helpers import results

# 真实作用域实测：300行命中的完整存档约需100万扫描工作量，预览与各次模型请求都在8万以内。
ARCHIVE_ONLY_LIMIT = 500_000
GREP = ("grep-call", "grep", {"query": "needle", "max_results": 2})
READ = ("read-call", "read_file", {"path": "notes.txt"})


def tool_calls(*calls: tuple[str, str, dict[str, Any]]) -> list[Any]:
    """模型在同一步里发出的若干调用。"""
    return [
        ResponseStarted(response_id="same-step"),
        *(
            ToolCallCompleted(call_id=call_id, tool=tool, arguments=arguments)
            for call_id, tool, arguments in calls
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def model_text(result: ToolResultContent) -> str:
    return tool_result_text(result.model_dump(mode="json", include={"outcome", "output", "error"}))


class _ResultTooLarge(SecretPublicationScope):
    """真实作用域；搜索结果本身的检查报告做不完（资源上限或10秒期限），其他检查照常。"""

    def __init__(self, code: str) -> None:
        provider = EnvironmentSecretProvider(
            (EnvironmentSecretSource("api", "9", "MODEL_KEY"),), environment={"MODEL_KEY": CANARY}
        )
        super().__init__((REFERENCE,), provider)
        self.code = code

    def assert_public_json(self, value: Any, *, checkpoint: Any) -> None:
        output = value.get("output") if isinstance(value, dict) else None
        if isinstance(output, dict) and "matches" in output:
            raise KernelError(self.code, "检查没有做完")
        super().assert_public_json(value, checkpoint=checkpoint)


async def _run(
    tmp_path: Path, step: list[Any], scope: SecretPublicationScope, *, text: str = "needle 中文"
) -> tuple[Path, ScriptedProvider, Turn]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text(f"{text}\n" * 300, encoding="utf-8")
    (root / "notes.txt").write_text("hello\n", encoding="utf-8")
    path = tmp_path / "session.db"
    store = SQLiteSessionStore(path)
    artifacts = SQLiteArtifactStore(store, public_output_protection=scope)
    provider = ScriptedProvider([step, answer("搜索完成")])
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "搜索", request_id="limit")
    return path, provider, turn


def _archives(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0]


async def test_search_archive_too_large_to_check_keeps_the_checked_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 修改前：整轮以public_output_limit失败，同一步的read_file没有结果。
    monkeypatch.setattr(publication, "MAX_SCAN_WORK", ARCHIVE_ONLY_LIMIT)
    with protected() as scope:
        path, provider, turn = await _run(tmp_path, tool_calls(GREP, READ), scope)

    assert turn.status is TurnStatus.COMPLETED, turn.error
    grep, read = results(turn)
    assert grep.outcome == "succeeded"
    assert isinstance(grep.output, dict)
    assert set(grep.output) == {"preview", "artifact_omitted"}
    assert grep.output["artifact_omitted"] == "protection_limit"
    assert len(grep.output["preview"]["matches"]) == 2
    assert "full results not saved (too large to check for protected values)" in model_text(grep)
    assert read.outcome == "succeeded"
    assert _archives(path) == 0
    assert len(provider.requests) == 2


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        ("trusted_action_output_limit", "public_output_limit"),
        ("public_output_timeout", "public_output_timeout"),
    ],
)
async def test_result_too_large_to_check_fails_only_that_call(
    tmp_path: Path, raised: str, code: str
) -> None:
    # 修改前：整轮以该码失败，同批并行的read_file记为失败。
    with _ResultTooLarge(raised) as scope:
        path, provider, turn = await _run(tmp_path, tool_calls(GREP, READ), scope)

    assert turn.status is TurnStatus.COMPLETED, turn.error
    grep, read = results(turn)
    assert grep.outcome == "failed" and grep.output is None
    assert grep.error is not None and grep.error.code == code
    assert grep.error.message == PROTECTION_LIMIT_FEEDBACK
    assert "needle" not in grep.model_dump_json()
    assert PROTECTION_LIMIT_FEEDBACK in model_text(grep)
    assert read.outcome == "succeeded"
    assert _archives(path) == 0
    assert len(provider.requests) == 2


async def test_a_protected_value_in_a_result_still_fails_the_turn(tmp_path: Path) -> None:
    with protected() as scope:
        _, provider, turn = await _run(tmp_path, tool_calls(GREP), scope, text=f"needle {CANARY}")

    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "public_output_secret_leak"
    assert CANARY not in turn.model_dump_json()
    assert len(provider.requests) == 1
