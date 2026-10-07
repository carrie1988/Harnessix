"""专用 SQL 窗口的连接关闭与首失败身份；不是业务授权或认证历史证据。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window


def test_closed_connection_cleanup_preserves_first_checkpoint_error():
    marker = KernelError("git_prepared_link_host_invalid", "原连接已经关闭")
    closed = False

    def checkpoint():
        if closed:
            raise marker

    with closing(sqlite3.connect(":memory:")) as db:
        with pytest.raises(KernelError) as caught:
            with git_prefix_sql_window(db, checkpoint=checkpoint):
                db.close()
                closed = True
        assert caught.value is marker


@pytest.mark.parametrize("error", [ValueError("original"), TimeoutError("original")])
def test_closed_connection_cleanup_preserves_body_error(error):
    with closing(sqlite3.connect(":memory:")) as db:
        with pytest.raises(type(error)) as caught:
            with git_prefix_sql_window(db, checkpoint=lambda: None):
                db.close()
                raise error
        assert caught.value is error
