from __future__ import annotations

from harnessix import cli


def test_license_command_is_offline_and_configuration_free(capsys) -> None:
    # 顶层CLI不再导入旧Action Settings；license因此不可能触发其环境解析。
    assert not hasattr(cli, "Settings")
    cli.main(["license"])

    assert capsys.readouterr().out == (
        "Harnessix Code\n"
        "Copyright © 2026 Zhang Jinhui\n"
        "社区许可证：AGPL-3.0-only\n"
        "源代码：https://github.com/carrie1988/Harnessix\n"
        "商业许可：https://github.com/carrie1988/Harnessix/blob/main/COMMERCIAL_LICENSE.md\n"
    )
