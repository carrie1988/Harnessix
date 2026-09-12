from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from harnessix.product_ui.controller import (
    ControllerPhase,
    CreateThreadIntent,
    ProductController,
    StartRequest,
    SubmitPromptIntent,
)
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.contracts import PublicTextContent
from harnessix.sdk import SubprocessAgentTransport


async def _wait_for_completion(controller: ProductController) -> None:
    for _ in range(100):
        view = controller.state.thread_view
        if (
            view is not None
            and view.current_turn is not None
            and view.current_turn.status == "completed"
        ):
            return
        await asyncio.wait_for(controller.next_update(), timeout=2)
    raise AssertionError("真实stdio Turn未完成")


async def test_controller_recovers_cold_transcript_through_restarted_stdio_process(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client_state = tmp_path / "client"
    database = tmp_path / "sessions.db"
    command = (
        sys.executable,
        "-m",
        "tests.product_ui.stdio_server",
        str(database),
        str(workspace),
    )

    with ClientStateStore(
        client_state,
        workspace_identity=str(workspace.resolve()),
    ) as store:
        first = ProductController(
            RecoverableAgentSession(store, lambda: SubprocessAgentTransport(command))
        )
        await first.start(StartRequest(workspace=str(workspace)))
        await first.dispatch(CreateThreadIntent())
        thread_id = first.state.selected_thread_id
        assert thread_id is not None
        await first.dispatch(SubmitPromptIntent("验证真实stdio冷恢复"))
        await _wait_for_completion(first)
        first_report = await first.close(deadline_seconds=5)
        assert first_report.clean
        assert store.state().next_command_sequence == 3

    with ClientStateStore(
        client_state,
        workspace_identity=str(workspace.resolve()),
    ) as reopened:
        second = ProductController(
            RecoverableAgentSession(reopened, lambda: SubprocessAgentTransport(command))
        )
        restored = await second.start(
            StartRequest(workspace=str(workspace), resume_thread_id=thread_id)
        )

        assert restored.phase is ControllerPhase.READY
        assert restored.selected_thread_id == thread_id
        assert restored.thread_view is not None
        texts = [
            item.item.content.text
            for item in restored.thread_view.items
            if isinstance(item.item.content, PublicTextContent)
        ]
        assert texts == ["验证真实stdio冷恢复", "stdio恢复完成"]
        assert reopened.state().next_command_sequence == 3
        assert (await second.close(deadline_seconds=5)).clean
