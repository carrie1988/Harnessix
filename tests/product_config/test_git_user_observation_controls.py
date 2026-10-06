"""用户观察控制边界单元回归；模拟转换端口不作为 Windows 原生验收。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_baseline import _root_binding_matches
from harnessix.product_config.git_user_observation_paths import (
    pin_git_user_directories,
    reported_git_path,
)


@pytest.mark.parametrize("error", [OSError("合成控制异常"), TimeoutError("合成控制期限")])
def test_index_checkpoint_crosses_oserror_converter_with_original_identity(
    tmp_path, monkeypatch, error
):
    """仿原 Windows 转换器仅捕获 OSError，检查点来源标记必须穿越该边界。"""
    (tmp_path / "index").write_bytes(b"fixture index")
    with pin_git_user_directories(tmp_path, tmp_path, checkpoint=lambda: None) as pinned:
        native_type = type(pinned.index)

        def converted(_self, _path, *, access, checkpoint):
            assert access == "read"
            try:
                checkpoint()
            except OSError:
                raise KernelError("workspace_observation_failed", "原生转换失败") from None
            pytest.fail("控制异常被吞掉")

        monkeypatch.setattr(native_type, "observe", converted)
        calls = 0

        def controlled():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise error

        with pytest.raises(type(error)) as caught:
            pinned.observe_index(controlled)
        assert caught.value is error and calls == 3


@pytest.mark.parametrize("error", [OSError("合成目录控制异常"), TimeoutError("合成目录期限")])
def test_root_capture_consumes_checkpoint_during_native_directory_enumeration(tmp_path, error):
    """真实原生根捕获逐项消费检查，不等待完整目录枚举结束才发现失效。"""
    for number in range(40):
        (tmp_path / str(number)).write_bytes(b"fixture")
    source = SimpleNamespace(workspace=SimpleNamespace(platform="posix"))
    calls = 0

    def controlled():
        nonlocal calls
        calls += 1
        if calls == 20:
            raise error

    with pytest.raises(type(error)) as caught:
        _root_binding_matches(source, tmp_path, controlled)
    assert caught.value is error and calls == 20


@pytest.mark.parametrize(
    "body",
    [b"", b".git", b".git\n\n", b"../outside\n", b"a\x00b\n", b"\xff\n", b"x" * 4096 + b"\n"],
)
def test_report_errors_do_not_export_untrusted_path_bytes(tmp_path, body):
    with pytest.raises(KernelError) as caught:
        reported_git_path(body, tmp_path)
    assert caught.value.code == "git_user_observation_path_invalid"
    assert str(tmp_path) not in str(caught.value)
