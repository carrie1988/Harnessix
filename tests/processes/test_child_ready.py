from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.processes.child_ready import CHILD_READY_PROGRAM


@pytest.mark.parametrize("interrupted", (None, "empty", "partial", "complete"))
def test_child_pid_closes_before_separate_ready_marker(tmp_path, monkeypatch, interrupted):
    marker = tmp_path / "started"
    pid_file = marker.with_name(marker.name + ".pid")
    monkeypatch.setattr(sys, "argv", ["bootstrap", str(marker)])
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=123456))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    write_text = Path.write_text

    def interrupt_write(path, value, *args, **kwargs):
        # 模拟写入前、部分写入及完成正文后中断；不伪造正式Owner终态。
        if interrupted is not None:
            write_text(path, {"empty": "", "partial": "12", "complete": value}[interrupted])
            raise OSError("bootstrap_interrupted")
        return write_text(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", interrupt_write)
    if interrupted is not None:
        with pytest.raises(OSError, match="bootstrap_interrupted"):
            exec(CHILD_READY_PROGRAM, {})
        assert not marker.exists()
    else:
        exec(CHILD_READY_PROGRAM, {})
        assert marker.read_bytes() == b""
        assert pid_file.read_bytes() == b"123456"
