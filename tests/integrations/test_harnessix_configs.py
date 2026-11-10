"""Harbor任务容器内Harnessix配置生成：不依赖Harbor包，本地全量回归即可覆盖。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessix.product_config.action_contracts import ProductActionConfigV1
from harnessix.product_config.contracts import ProductConfigV2
from integrations.harbor import harnessix_configs
from integrations.harbor.harnessix_configs import (
    CONTEXT_WINDOW_TOKENS,
    action_config,
    apply_eval_overrides,
    product_config,
    task_action_config,
)
from integrations.harbor.task_environment import (
    MODEL,
    parse_environ,
    task_command_environment,
    task_language,
)


def test_harbor_product_config_declares_the_local_model_window() -> None:
    profile = json.loads(product_config(18001))["profiles"][0]
    assert profile["context_window_tokens"] == CONTEXT_WINDOW_TOKENS[MODEL] == 262_144
    other = json.loads(product_config(18001, model="other-model"))["profiles"][0]
    assert "context_window_tokens" not in other


def test_the_harbor_arm_sends_compact_tool_results():
    """评测组别沿用产品向导的默认值：紧凑工具结果开启。"""
    provider = json.loads(product_config(18001))["providers"][0]
    assert provider["kind"] == "openai_chat" and provider["compact_tool_results"] is True


def test_the_harbor_arm_turns_on_the_completion_guard():
    """评测组别是无人值守任务：纯文本提前收工改为补发继续提示；这是唯一允许的覆盖项。"""
    config = json.loads(product_config(18001))
    assert config["completion_guard"] is True


def test_eval_overrides_reject_fields_outside_the_whitelist():
    config = ProductConfigV2.model_validate_json(product_config(18001))
    assert apply_eval_overrides(config, {"completion_guard": False}).completion_guard is False
    with pytest.raises(SystemExit, match="workspace_read_timeout_seconds"):
        apply_eval_overrides(config, {"workspace_read_timeout_seconds": 60})


def test_harnessix_arms_only_use_known_agent_options():
    """新增评测选项必须先在这里登记，避免评测悄悄用上用户拿不到的配置。"""
    allowed = {
        "action_config",
        "action_config_template",
        "approval_mode",
        "bundle_root",
        "external_boundary",
        "install_bundle",
        "max_steps",
        "max_tokens",
        "model_relays",
        # 评测基础设施：智能体运行阶段断开外网，各组一致，不是产品配置。
        "network_isolation",
        "normalize_workspace_modes",
        # 评测基础设施：命令环境取任务容器自身的环境（工具链）叠加语言预设，见task_environment。
        "task_command_environment",
        # 任务模板是已知差异，由不带模板的产品默认组对照。
        "prompt_template_path",
        "python_executable",
        "server_config",
        "server_config_template",
        "state_directory",
        "timeout_seconds",
        "workspace",
    }
    root = Path(__file__).resolve().parents[2] / "benchmarks" / "harbor"
    seen: set[str] = set()
    for path in root.rglob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        arms = document.get("arms", []) if isinstance(document, dict) else []
        for arm in arms:
            if "harnessix" in str(arm.get("system", "")).lower():
                seen |= set(arm["agent"].get("kwargs", {}))
    assert seen and seen <= allowed, sorted(seen - allowed)


# 任务镜像的ENV（取自Multilingual的Ruby与Go镜像），外加Docker给容器加的HOSTNAME与HOME。
RUBY_IMAGE = {
    "PATH": "/usr/local/bundle/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "GEM_HOME": "/usr/local/bundle",
    "BUNDLE_APP_CONFIG": "/usr/local/bundle",
    "HOSTNAME": "0123456789ab",
    "HOME": "/root",
}
GO_IMAGE = {
    "PATH": "/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "TZ": "Etc/UTC",
    "HOSTNAME": "0123456789ab",
    "HOME": "/root",
}


def _profile_environment(body: str) -> dict[str, str]:
    config = ProductActionConfigV1.model_validate_json(body)
    return dict(config.native_process_profiles[0].environment)


def test_task_language_comes_from_the_harbor_language_tag() -> None:
    assert task_language(["debugging", "swe-bench", "swe-bench-multilingual", "go"]) == "go"
    assert task_language(["debugging", "swe-bench", "swe-bench-multilingual", "cpp"]) == "cpp"
    # SWE-bench原版（Verified）任务没有语言标签。
    assert task_language(["debugging", "swe-bench"]) == "python"
    with pytest.raises(ValueError, match="多个语言标签"):
        task_language(["go", "rust"])


def test_task_environment_keeps_the_image_toolchain_under_the_language_presets() -> None:
    """外部边界命令不继承容器环境：任务环境把镜像ENV写进Profile，语言预设覆盖同名变量。"""
    environment = dict(task_command_environment(GO_IMAGE, "go"))
    assert environment["PATH"].startswith("/usr/local/go/bin:")
    assert environment["TZ"] == "Etc/UTC"
    assert environment["HOME"] == "/tmp" and environment["GOPROXY"] == "off"
    assert "HOSTNAME" not in environment
    online = {**GO_IMAGE, "GOPROXY": "https://proxy.golang.org", "XDG_CACHE_HOME": "/root/.cache"}
    overridden = dict(task_command_environment(online, "go"))
    assert (overridden["GOPROXY"], overridden["XDG_CACHE_HOME"]) == ("off", "/tmp/.cache")
    assert "MODEL_API_KEY" not in dict(
        task_command_environment({**GO_IMAGE, "MODEL_API_KEY": "secret"}, "go")
    )
    with pytest.raises(ValueError, match="没有该语言"):
        task_command_environment(GO_IMAGE, "kotlin")


def test_task_environment_follows_the_image_locale() -> None:
    """产品基础环境的LC_ALL=C会盖过镜像的LANG（Ruby默认编码变成US-ASCII）；镜像只设LANG时跟随它。"""
    ruby = dict(task_command_environment(RUBY_IMAGE, "ruby"))
    assert ruby["LANG"] == ruby["LC_ALL"] == "C.UTF-8"
    assert ruby["GEM_HOME"] == "/usr/local/bundle"
    assert "LC_ALL" not in dict(task_command_environment(GO_IMAGE, "go"))
    java = {**GO_IMAGE, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    assert dict(task_command_environment(java, "java"))["LC_ALL"] == "en_US.UTF-8"


def test_task_action_config_is_a_valid_profile_and_reports_dropped_names() -> None:
    container = {**RUBY_IMAGE, "http_proxy": "http://proxy", "HARNESSIX_X": "1", "bad-name": "v"}
    body, dropped = task_action_config(container, "ruby")
    assert dropped == ["HARNESSIX_X", "bad-name", "http_proxy"]
    environment = _profile_environment(body)
    assert environment["PATH"] == RUBY_IMAGE["PATH"]
    assert not {"HARNESSIX_X", "bad-name", "http_proxy", "HOSTNAME"} & set(environment)
    # 值不合规不悄悄略去：本试次安装失败。
    with pytest.raises(ValueError):
        task_action_config({**RUBY_IMAGE, "MULTI": "a\nb"}, "ruby")


def test_the_static_python_actions_stay_unchanged() -> None:
    """Verified组别用的固定actions.json不受任务环境影响。"""
    assert _profile_environment(action_config("python")) == {
        "HOME": "/tmp",
        "PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:"
        "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_CACHE_HOME": "/tmp/.cache",
    }


def test_generator_cli_writes_the_task_actions_from_a_proc_environ_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = tmp_path / "environ"
    environ.write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in GO_IMAGE.items()) + b"\0")
    assert parse_environ(environ.read_bytes()) == GO_IMAGE
    target = tmp_path / "etc" / "actions.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "harnessix_configs.py",
            "--language",
            "go",
            "--container-environment",
            str(environ),
            "--actions-file",
            str(target),
        ],
    )
    harnessix_configs.main()
    assert json.loads(capsys.readouterr().out)["language"] == "go"
    assert _profile_environment(target.read_text())["GOFLAGS"] == "-mod=mod"
    assert target.stat().st_mode & 0o777 == 0o600
