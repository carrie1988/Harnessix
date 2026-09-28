"""仅原生失败的低敏诊断：固定位置、异常类别与数值错误码，不输出正文或路径。"""

from __future__ import annotations

import ctypes
import json
from contextlib import contextmanager

from harnessix.product_config import state_backup as backup
from harnessix.product_config.session_key_windows_files import WindowsKeyFiles


def install_backup_diagnostics(monkeypatch):
    original_publish = WindowsKeyFiles.publish
    original_errors = backup._backup_errors

    def publish(self, source, target):
        try:
            original_publish(self, source, target)
        except Exception as error:
            _emit("directory-publication", error, ctypes.__dict__["get_last_error"]())
            raise

    @contextmanager
    def errors():
        with original_errors():
            try:
                yield
            except Exception as error:
                _emit("complete-backup", error)
                raise

    monkeypatch.setattr(WindowsKeyFiles, "publish", publish)
    monkeypatch.setattr(backup, "_backup_errors", errors)


def _emit(location, error, native_error=None):
    operations = []
    trace = error.__traceback__
    allowed = {
        "_backup",
        "_quiet_databases",
        "_copy_database",
        "_manifest",
        "publish_tree",
        "file_revisions",
        "original_key",
        "validate_product_state",
        "write_new",
    }
    while trace:
        name = trace.tb_frame.f_code.co_name
        if name in allowed:
            operations.append(name)
        trace = trace.tb_next
    facts = {
        "spec_version": "harnessix.windows-backup-diagnostic/v1",
        "location": location,
        "exception_type": type(error).__name__,
        "operations": operations,
    }
    for name, value in [
        ("native_error", native_error),
        *((name, getattr(error, name, None)) for name in ("errno", "winerror", "sqlite_errorcode")),
    ]:
        if type(value) is int:
            facts[name] = value
    print("WINDOWS_BACKUP_FAILURE_FACTS=" + json.dumps(facts))
