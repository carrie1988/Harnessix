"""原两例的独立调试入口；仅截取pytest发布的有限帧，不增加原13个hook。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

# 与原仓库根python -m pytest相同的模块发现起点；不改变生产子进程环境。
sys.path.insert(0, str(Path.cwd().resolve(strict=True)))

from scripts.windows_git_native_branch_observation.contract import read_contract  # noqa: E402
from scripts.windows_git_native_branch_observation.failure_projection import (  # noqa: E402
    SIBLING_KEY,
    failure_report,
    project_failure_case,
)
from scripts.windows_git_native_branch_observation.projection import (  # noqa: E402
    PROBE_PREFIX,
    project_case,
)


class CaseSink(io.TextIOBase):
    """丢弃非协议文本；任何超限或坏帧均令见证不完整，不写异常正文。"""

    def __init__(self) -> None:
        self.pending = ""
        self.rows: list[dict] = []
        self.failure_rows: list[dict] = []
        self.invalid = False

    def write(self, text: str) -> int:
        if len(text) > 65536:
            self.pending = ""
            self.invalid = True
            return len(text)
        if text.startswith(PROBE_PREFIX) and self.pending:
            if PROBE_PREFIX in self.pending or any(
                self.pending.endswith(PROBE_PREFIX[:size]) for size in range(1, len(PROBE_PREFIX))
            ):
                self.invalid = True
            else:
                # 原探针单次write构成边界；只清非协议噪声，未完协议冻结invalid并保留。
                self.pending = ""
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.startswith(PROBE_PREFIX):
                try:
                    if len(self.rows) >= 2:
                        raise ValueError("extra_probe_frame")
                    self.rows.append(project_case(line))
                    self.failure_rows.append(project_failure_case(line))
                except (ValueError, TypeError, RecursionError, UnicodeError):
                    self.invalid = True
        if len(self.pending) > 65536:
            self.pending = ""
            self.invalid = True
        return len(text)


def main(output: Path) -> int:
    import pytest

    contract = read_contract()
    sink = CaseSink()
    arguments = [
        "-p",
        "tests.product_config.git_minimum_commit_probe",
        "-q",
        "-s",
        "--tb=no",
        "-rN",
        "--show-capture=no",
        "--disable-warnings",
        "--no-header",
        "--basetemp",
        str(output / "fixture"),
        "--git-material-trace2=stderr-event-v1",
        *contract["selectors"],
    ]
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        code = int(pytest.main(arguments))
    report = {
        "pytest_exit": code,
        "cases": sink.rows,
        "invalid": sink.invalid or sink.pending.startswith(PROBE_PREFIX),
        SIBLING_KEY: failure_report(sink.failure_rows),
    }
    (output / "cases.json").write_text(json.dumps(report) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve(strict=True)))
