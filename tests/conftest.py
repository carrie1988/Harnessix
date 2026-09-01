from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from harnessix.bootstrap import build_service
from harnessix.runtime import ActionService
from harnessix.settings import Settings


@pytest_asyncio.fixture
async def service(tmp_path: Path) -> ActionService:
    action_service = build_service(Settings(database_path=tmp_path / "harnessix.db"))
    await action_service.initialize()
    return action_service
