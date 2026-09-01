from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path = Path(".harnessix/harnessix.db")
    host: str = "127.0.0.1"
    port: int = 8787
    lease_seconds: int = 30

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            database_path=Path(os.getenv("HARNESSIX_DATABASE_PATH", ".harnessix/harnessix.db")),
            host=os.getenv("HARNESSIX_HOST", "127.0.0.1"),
            port=int(os.getenv("HARNESSIX_PORT", "8787")),
            lease_seconds=int(os.getenv("HARNESSIX_LEASE_SECONDS", "30")),
        )
