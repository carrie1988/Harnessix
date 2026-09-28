"""有限诊断不得回显原错误正文，也不能替换原备份拒绝或吞异常。"""

from __future__ import annotations

import ctypes
import json

import pytest

from harnessix.product_config import state_backup as backup
from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
from tests.product_config.windows_backup_diagnostics import install_backup_diagnostics


@pytest.mark.parametrize("location", ["publication", "backup"])
def test_native_diagnostics_keep_original_error_and_exclude_body(monkeypatch, capsys, location):
    original_error = OSError(32, "PRIVATE-DIAGNOSTIC-BODY /private/canary/not-for-log")

    def fail(*args):
        raise original_error

    monkeypatch.setitem(ctypes.__dict__, "get_last_error", lambda: 32)
    monkeypatch.setattr(WindowsKeyFiles, "publish", fail)
    install_backup_diagnostics(monkeypatch)
    if location == "publication":
        with pytest.raises(OSError) as caught:
            object.__new__(WindowsKeyFiles).publish(None, None)
        assert caught.value is original_error
    else:
        with pytest.raises(backup.KernelError) as caught:
            with backup._backup_errors():
                raise original_error
        assert caught.value.code == "product_backup_invalid"
    output = capsys.readouterr().out
    assert "PRIVATE-DIAGNOSTIC-BODY" not in output and "/private/" not in output
    facts = json.loads(output.removeprefix("WINDOWS_BACKUP_FAILURE_FACTS="))
    assert facts["errno"] == 32 and facts["exception_type"] == type(original_error).__name__
