from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from harnessix.bootstrap import build_service
from harnessix.runtime import ActionService
from harnessix.settings import Settings


@pytest_asyncio.fixture
async def service(tmp_path: Path) -> AsyncIterator[ActionService]:
    action_service = build_service(
        Settings(
            database_path=tmp_path / "harnessix.db",
            demo_database_path=tmp_path / "demo-external.db",
        )
    )
    await action_service.initialize()
    try:
        yield action_service
    finally:
        await action_service.close()
