"""Harbor任务容器内Harnessix配置生成：不依赖Harbor包，本地全量回归即可覆盖。"""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path

import pytest

from harnessix.product_config.action_contracts import ProductActionConfigV1
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.contracts import ProductConfigV2
from integrations.harbor import harnessix_configs
from integrations.harbor.harnessix_configs import (
    CONTEXT_WINDOW_TOKENS,
    EVAL_ALLOWED_OVERRIDES,
    action_config,
    apply_eval_overrides,
    apply_eval_profile_overrides,
    container_user_home,
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
    """评测组别是无人值守任务：纯文本提前收工改为补发继续提示。"""
    config = json.loads(product_config(18001))
    assert config["completion_guard"] is True


def test_eval_overrides_are_the_documented_eval_product_differences():
    """评测只覆盖已登记、有文档记录的差异；新增覆盖项必须先在这里登记。"""
    assert EVAL_ALLOWED_OVERRIDES == {"completion_guard"}


def test_the_harbor_arm_compacts_at_the_product_default_point(tmp_path: Path) -> None:
    """评测测到的就是产品默认的压缩点（ADR 0132）：不再为累计Token预算提前到窗口三分之一。

    Multilingual正式评测中三分之一档实际在约5.3万Token（窗口的20%）触发，48次压缩约占全场12%的时间。
    """
    serialized = product_config(18001)
    assert "compaction" not in json.loads(serialized)
    config = ProductConfigV2.model_validate_json(serialized)
    assert config.compaction == "half_window"
    profile = config.profiles[0]
    assert profile.context_window_tokens == 262_144 and profile.max_output_tokens == 16_384
    evaluated = build_product_agent_context(
        tmp_path,
        max_output_tokens=profile.max_output_tokens,
        context_window_tokens=profile.context_window_tokens,
        context_compaction=config.compaction,
    )
    product_default = build_product_agent_context(
        tmp_path,
        max_output_tokens=profile.max_output_tokens,
        context_window_tokens=profile.context_window_tokens,
    )
    assert evaluated.compaction == product_default.compaction
    assert evaluated.compaction_window == product_default.compaction_window
    assert evaluated.compaction.trigger_history_tokens == 393_216
    assert evaluated.compaction.policy.target_history_tokens == 196_608


def test_eval_overrides_reject_fields_outside_the_whitelist():
    config = ProductConfigV2.model_validate_json(product_config(18001))
    assert apply_eval_overrides(config, {"completion_guard": False}).completion_guard is False
    with pytest.raises(SystemExit, match="workspace_read_timeout_seconds"):
        apply_eval_overrides(config, {"workspace_read_timeout_seconds": 60})
    # 压缩点不再是登记的差异：评测不能再悄悄换回更早的压缩点。
    with pytest.raises(SystemExit, match="compaction"):
        apply_eval_overrides(config, {"compaction": "third_window"})


def test_harnessix_arms_only_use_known_agent_options():
    """新增评测选项必须先在这里登记，避免评测悄悄用上用户拿不到的配置。"""
    allowed = {
        "action_config",
        "action_config_template",
        "approval_mode",
        "bundle_root",
        "external_boundary",
        # 让评测组别获得用户默认就有的Git读工具与会话基线（ADR 0131）。
        "git_read_tools",
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


def test_the_harbor_arm_allows_longer_single_responses():
    """评测每步输出上限16384（产品自托管默认4096），流式分块与响应字节上限足以容纳用满的输出。"""
    profile = json.loads(product_config(18001))["profiles"][0]
    assert profile["max_output_tokens"] == 16_384
    assert profile["max_chunks"] >= profile["max_output_tokens"]
    assert profile["max_response_bytes"] >= 128 * profile["max_output_tokens"]


def test_eval_profile_overrides_reject_fields_outside_the_whitelist():
    config = ProductConfigV2.model_validate_json(product_config(18001))
    assert (
        apply_eval_profile_overrides(config, {"max_output_tokens": 8192})
        .profiles[0]
        .max_output_tokens
        == 8192
    )
    with pytest.raises(SystemExit, match="timeout_seconds"):
        apply_eval_profile_overrides(config, {"timeout_seconds": 60})


# docker exec进程看到的容器环境（取自Multilingual的Ruby与Go镜像），外加Docker补上的HOSTNAME与HOME。
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
# 一期为/root不可写的沙箱把HOME与缓存挪到/tmp；外部边界的任务环境不得再设这些位置。
RELOCATED = {
    "HOME",
    "XDG_CACHE_HOME",
    "GOCACHE",
    "GOMODCACHE",
    "GRADLE_USER_HOME",
    "GRADLE_RO_DEP_CACHE",
    "MAVEN_USER_HOME",
    "COREPACK_HOME",
    "npm_config_cache",
    "YARN_CACHE_FOLDER",
}


def _profile_environment(body: str) -> dict[str, str]:
    config = ProductActionConfigV1.model_validate_json(body)
    return dict(config.native_process_profiles[0].environment)


def _task(container: dict[str, str], language: str, home: str = "/root") -> dict[str, str]:
    return dict(task_command_environment(container, language, home=home))


def test_task_language_comes_from_the_harbor_language_tag() -> None:
    assert task_language(["debugging", "swe-bench", "swe-bench-multilingual", "go"]) == "go"
    assert task_language(["debugging", "swe-bench", "swe-bench-multilingual", "cpp"]) == "cpp"
    # SWE-bench原版（Verified）任务没有语言标签。
    assert task_language(["debugging", "swe-bench"]) == "python"
    with pytest.raises(ValueError, match="多个语言标签"):
        task_language(["go", "rust"])


@pytest.mark.parametrize(
    "language", ["go", "rust", "javascript", "typescript", "java", "php", "ruby", "cpp", "python"]
)
def test_task_environment_keeps_the_image_home_and_cache_locations(language: str) -> None:
    """外部边界下任务容器即边界、以root运行：不再沿用一期把HOME与缓存挪到/tmp的预设，
    与mini一样用镜像自身的HOME、热缓存与配置；镜像ENV声明的位置原样保留。"""
    environment = _task(GO_IMAGE, language)
    assert environment["HOME"] == "/root"
    assert not (RELOCATED - {"HOME"}) & set(environment)
    assert not any(value.startswith("/tmp") for value in environment.values())
    declared = {**GO_IMAGE, "XDG_CACHE_HOME": "/root/.cache", "GOCACHE": "/root/.cache/go-build"}
    kept = _task(declared, language)
    assert (kept["XDG_CACHE_HOME"], kept["GOCACHE"]) == ("/root/.cache", "/root/.cache/go-build")


def _process(tmp_path: Path, environment: dict[str, str]) -> Path:
    process = tmp_path / "proc-1"
    process.mkdir(parents=True)
    body = b"\0".join(f"{k}={v}".encode() for k, v in environment.items()) + b"\0"
    (process / "environ").write_bytes(body)
    return process


def test_home_is_the_default_users_not_the_generators(tmp_path: Path) -> None:
    """生成器以root运行，HOME取容器默认用户（主进程）的：镜像或Docker给的HOME，没有时取passwd家目录，
    不是/tmp。"""
    assert _task(GO_IMAGE, "go", home="/home/agent")["HOME"] == "/home/agent"
    assert container_user_home(_process(tmp_path / "a", {"HOME": "/custom", "PATH": "/bin"})) == (
        "/custom"
    )
    passwd = pwd.getpwuid(os.getuid()).pw_dir
    assert container_user_home(_process(tmp_path / "b", {"PATH": "/bin"})) == passwd


def test_agent_injected_and_shell_variables_are_not_container_environment() -> None:
    noisy = {**GO_IMAGE, "OPENAI_API_KEY": "secret", "PWD": "/testbed", "SHLVL": "1", "_": "/x"}
    environment = dict(
        task_command_environment(noisy, "go", home="/root", excluded=("OPENAI_API_KEY",))
    )
    assert not {"OPENAI_API_KEY", "PWD", "SHLVL", "_", "HOSTNAME"} & set(environment)
    assert "MODEL_API_KEY" not in _task({**GO_IMAGE, "MODEL_API_KEY": "secret"}, "go")


def test_task_environment_adds_only_offline_switches() -> None:
    """离线开关以评测值为准；GOFLAGS只在镜像未设时补-mod=mod；MAVEN_ARGS保留镜像参数再追加。"""
    go = _task({**GO_IMAGE, "GOPROXY": "https://proxy.golang.org"}, "go")
    assert {k: go[k] for k in ("GOPROXY", "GOSUMDB", "GOTOOLCHAIN", "GOFLAGS")} == {
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=mod",
    }
    assert _task({**GO_IMAGE, "GOFLAGS": "-mod=vendor"}, "go")["GOFLAGS"] == "-mod=vendor"
    assert set(_task(GO_IMAGE, "go")) - set(GO_IMAGE) == {
        "GOFLAGS",
        "GOPROXY",
        "GOSUMDB",
        "GOTOOLCHAIN",
    }
    assert _task(GO_IMAGE, "java")["MAVEN_ARGS"] == "--offline"
    assert _task({**GO_IMAGE, "MAVEN_ARGS": "-B -ntp"}, "java")["MAVEN_ARGS"] == "-B -ntp --offline"
    assert _task({**GO_IMAGE, "MAVEN_ARGS": "--offline"}, "java")["MAVEN_ARGS"] == "--offline"
    javascript = _task(GO_IMAGE, "javascript")
    assert (javascript["npm_config_offline"], javascript["YARN_ENABLE_NETWORK"]) == ("true", "0")
    assert javascript["COREPACK_ENABLE_NETWORK"] == "0"
    assert _task(GO_IMAGE, "rust")["CARGO_NET_OFFLINE"] == "true"
    assert _task(GO_IMAGE, "php")["COMPOSER_DISABLE_NETWORK"] == "1"
    assert _task(GO_IMAGE, "cpp") == {k: v for k, v in GO_IMAGE.items() if k != "HOSTNAME"}
    with pytest.raises(ValueError, match="没有该语言"):
        task_command_environment(GO_IMAGE, "kotlin", home="/root")


def test_task_environment_follows_the_image_locale() -> None:
    """产品基础环境的LC_ALL=C会盖过镜像的LANG（Ruby默认编码变成US-ASCII）；镜像只设LANG时跟随它。"""
    ruby = _task(RUBY_IMAGE, "ruby")
    assert ruby["LANG"] == ruby["LC_ALL"] == "C.UTF-8"
    assert ruby["GEM_HOME"] == "/usr/local/bundle"
    assert "LC_ALL" not in _task(GO_IMAGE, "go")
    java = {**GO_IMAGE, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    assert _task(java, "java")["LC_ALL"] == "en_US.UTF-8"


def test_task_action_config_is_a_valid_profile_and_reports_dropped_names() -> None:
    container = {**RUBY_IMAGE, "http_proxy": "http://proxy", "HARNESSIX_X": "1", "bad-name": "v"}
    body, dropped = task_action_config(container, "ruby", home="/root")
    assert dropped == ["HARNESSIX_X", "bad-name", "http_proxy"]
    environment = _profile_environment(body)
    assert environment["PATH"] == RUBY_IMAGE["PATH"]
    assert not {"HARNESSIX_X", "bad-name", "http_proxy", "HOSTNAME"} & set(environment)
    # 值不合规不悄悄略去：本试次安装失败。
    with pytest.raises(ValueError):
        task_action_config({**RUBY_IMAGE, "MULTI": "a\nb"}, "ruby", home="/root")


def test_the_static_python_actions_stay_unchanged() -> None:
    """Verified组别用的固定actions.json（一期预设，dev25结果即用它）不受任务环境影响。"""
    assert _profile_environment(action_config("python")) == {
        "HOME": "/tmp",
        "PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:"
        "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_CACHE_HOME": "/tmp/.cache",
    }


def test_generator_cli_reads_the_exec_environment_and_the_default_users_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """容器内用法：环境取生成器这一docker exec进程（/proc/self/environ），去掉--exclude；
    HOME取主进程（/proc/1）的。"""
    exec_environment = {**GO_IMAGE, "MODEL_API_KEY": "x", "OPENAI_API_KEY": "y"}
    environ = _process(tmp_path / "self", exec_environment) / "environ"
    assert parse_environ(environ.read_bytes()) == exec_environment
    main_process = _process(tmp_path / "one", {"HOME": "/home/agent", "PATH": "/bin"})
    target = tmp_path / "etc" / "actions.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "harnessix_configs.py",
            "--language",
            "go",
            "--container-environment",
            str(environ),
            "--container-user-process",
            str(main_process),
            "--exclude=OPENAI_API_KEY",
            "--actions-file",
            str(target),
        ],
    )
    harnessix_configs.main()
    assert json.loads(capsys.readouterr().out)["language"] == "go"
    environment = _profile_environment(target.read_text())
    assert environment["HOME"] == "/home/agent"
    assert environment["GOFLAGS"] == "-mod=mod"
    assert not {"MODEL_API_KEY", "OPENAI_API_KEY"} & set(environment)
    assert target.stat().st_mode & 0o777 == 0o600
