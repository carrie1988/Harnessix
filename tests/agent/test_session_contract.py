from pathlib import Path

import pytest

from harnessix.session.sqlite import SQLiteSessionStore
from tests.contracts.session import SessionStoreContract, StoreFactory


@pytest.fixture
def store_factory(tmp_path: Path) -> StoreFactory:
    return lambda name: SQLiteSessionStore(tmp_path / f"{name}.db")


class TestSQLiteSessionContract(SessionStoreContract):
    pass
