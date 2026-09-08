from __future__ import annotations

from harnessix import cli


def test_license_command_is_offline_and_configuration_free(capsys, monkeypatch) -> None:
    def forbidden():
        raise AssertionError("license命令不得读取运行配置")

    monkeypatch.setattr(cli.Settings, "from_environment", forbidden)

    cli.main(["license"])

    assert capsys.readouterr().out == (
        "Harnessix Code\n"
        "Copyright © 2026 Zhang Jinhui\n"
        "社区许可证：AGPL-3.0-only\n"
        "源代码：https://github.com/carrie1988/Harnessix\n"
        "商业许可：https://github.com/carrie1988/Harnessix/blob/main/COMMERCIAL_LICENSE.md\n"
    )
