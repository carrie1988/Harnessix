"""命令行打印的JSON在输出流不是终端时只写ASCII（Beta Issue #26的做法推广到其余JSON输出）。

0.10.0b53 Windows正式测试：未开UTF-8模式时，`data export`的错误说明经管道按控制台代码页（cp936）
写出，调用方按UTF-8解码得到乱码。exec的摘要早已只写ASCII；data、doctor --json、config、state同样。
"""

from __future__ import annotations

import io
import json

import pytest

from harnessix.product_config.cli_json import is_terminal, print_json


class _Stream(io.StringIO):
    def __init__(self, *, terminal: bool) -> None:
        super().__init__()
        self._terminal = terminal

    def isatty(self) -> bool:
        return self._terminal


@pytest.mark.parametrize("terminal", [False, True])
def test_json_is_ascii_only_unless_the_stream_is_a_terminal(terminal: bool) -> None:
    stream = _Stream(terminal=terminal)
    value = {"code": "data_workspace_busy", "message": "请先在那里按 Ctrl+Q 退出"}
    print_json(value, file=stream, sort_keys=True)
    text = stream.getvalue()
    assert json.loads(text) == value and text.isascii() is not terminal


def test_a_stream_without_isatty_is_not_a_terminal() -> None:
    assert not is_terminal(object())  # type: ignore[arg-type]


def test_data_export_errors_are_ascii_when_piped(tmp_path, monkeypatch) -> None:
    """修改前这里输出原文中文；管道里只写ASCII，解析出的说明不变。"""
    from harnessix.product_ui import data_cli

    stream = _Stream(terminal=False)
    monkeypatch.setattr("sys.stdout", stream)
    with pytest.raises(SystemExit):
        data_cli.data_main(["delete", "--fingerprint", "f" * 64, "--confirm", "0" * 64])
    text = stream.getvalue()
    assert text.isascii() and json.loads(text)["code"] == "data_delete_unconfirmed"
