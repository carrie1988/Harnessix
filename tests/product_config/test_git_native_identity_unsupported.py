"""sqlite3不支持扩展加载的Python上，原生连接身份按不可用失败关闭，而不是抛出意外异常。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_native_identity as native


def test_connection_without_extension_loading_fails_closed() -> None:
    class Connection:
        """模拟uv在macOS上的独立Python：连接对象没有enable_load_extension。"""

    with pytest.raises(KernelError) as caught, native._extension_loading(Connection()):  # type: ignore[arg-type]
        pytest.fail("不支持扩展加载时不得进入加载区")
    assert caught.value.code == "git_prepared_link_host_invalid"
