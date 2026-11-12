"""启动失败的说明：先说原因，再给可照做的命令；不再直接打印整张检查清单（0.10.0b9）。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.product_contracts import ProductPreflightCheck, ProductPreflightReport
from harnessix.product_ui import startup_failure
from harnessix.product_ui.startup_failure import startup_failure_text
from tests.product_config.conftest import write_config


def _check(code: str, remediation: str | None, category="secret", status="failed"):
    return ProductPreflightCheck(
        check_id=f"product_{code}",
        category=category,
        requirement="required",
        status=status,
        code=code,
        remediation_id=remediation,
        duration_ms=0,
    )


def _report(*checks: ProductPreflightCheck) -> ProductPreflightReport:
    return ProductPreflightReport.model_construct(
        mode="startup",
        platform="posix",
        checks=checks,
        ready=False,
        generated_at=datetime.now(UTC),
        report_sha256="0" * 64,
    )


def test_missing_key_says_what_to_type_with_the_configured_variable(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = write_config(tmp_path / "config.json", config)
    names = [source.environment_variable for source in config.secret_sources]
    text = startup_failure_text(
        _report(
            _check("config_ready", None, category="config", status="passed"),
            _check("secret_unavailable", "set_environment_secret"),
        ),
        path,
    )
    assert text.startswith("Harnessix 无法启动：缺少模型的 API Key。")
    posix, powershell = f"export {names[0]}=你的API Key", f'$env:{names[0]}="你的API Key"'
    command = powershell if os.name == "nt" else posix
    assert command in text
    assert "2. 重新运行刚才的命令。" in text and "harnessix code doctor" in text
    assert "[通过]" not in text and "secret_unavailable" not in text


def test_windows_gets_powershell_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(startup_failure.os, "name", "nt")
    text = startup_failure_text(
        _report(_check("secret_unavailable", "set_environment_secret")), None
    )
    assert '$env:MODEL_API_KEY="你的API Key"' in text and "setx MODEL_API_KEY" in text


def test_several_problems_are_numbered_and_unknown_ones_keep_their_code() -> None:
    text = startup_failure_text(
        _report(
            _check("secret_unavailable", "set_environment_secret"),
            _check("something_new", None, category="workspace"),
        ),
        None,
    )
    assert text.startswith("Harnessix 无法启动，有 2 个问题需要处理：")
    assert "1. 缺少模型的 API Key" in text
    assert "2. 启动检查未通过（something_new）" in text


@pytest.mark.parametrize(
    ("platform", "profile"), [("darwin", "~/.bash_profile（bash）"), ("linux", "~/.bashrc（bash）")]
)
def test_the_persistent_key_hint_names_the_shell_file_each_platform_reads(
    monkeypatch: pytest.MonkeyPatch, platform: str, profile: str
) -> None:
    # Linux的bash交互Shell读~/.bashrc，照macOS写~/.bash_profile会不生效（2026-11-11三平台对照）。
    monkeypatch.setattr(startup_failure.os, "name", "posix")
    monkeypatch.setattr(startup_failure.sys, "platform", platform)
    assert profile in startup_failure.api_key_setup_text(("DEEPSEEK_API_KEY",))
