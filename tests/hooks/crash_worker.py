from __future__ import annotations

import os
import sys
from uuid import UUID

from harnessix.hooks.store import SQLiteHookStore


def main() -> None:
    store = SQLiteHookStore(sys.argv[1])
    store.transition(UUID(sys.argv[2]), expected={"ready"}, target="running")
    os._exit(73)


if __name__ == "__main__":
    main()
