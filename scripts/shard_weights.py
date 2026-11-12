"""从pytest输出生成CI分片用的慢文件耗时表（tests/shard_weights.json）。

用法：python scripts/shard_weights.py <pytest日志>...
日志需要带每项用例的耗时（pytest --durations=0 --durations-min=1），可以给多片的日志。
只登记合计不少于MIN_SECONDS秒的文件；其余文件由tests/conftest.py按路径均匀分散。
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

MIN_SECONDS = 20.0
OUTPUT = Path(__file__).resolve().parents[1] / "tests" / "shard_weights.json"
_DURATION = re.compile(r"(\d+\.\d+)s (?:call|setup|teardown)\s+(tests/[^:\s]+\.py)::")


def file_seconds(logs: list[Path]) -> dict[str, float]:
    totals: collections.Counter[str] = collections.Counter()
    for log in logs:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _DURATION.search(line)
            if match:
                totals[match[2]] += float(match[1])
    return {name: round(seconds, 1) for name, seconds in totals.items() if seconds >= MIN_SECONDS}


def main(arguments: list[str]) -> int:
    if not arguments:
        print(__doc__, file=sys.stderr)
        return 2
    files = file_seconds([Path(argument) for argument in arguments])
    document = {"unit": "seconds", "files": dict(sorted(files.items()))}
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"{len(files)}个文件，合计{sum(files.values()):.0f}秒 -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
