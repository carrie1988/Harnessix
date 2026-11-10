"""Harbor任务容器中Harnessix组别使用的本地模型标识与按语言的命令环境。

外部边界命令只得到Profile声明的变量，不继承App Server或容器的环境（ADR 0121），所以评测替每个任务
声明命令环境，有两种：
- 静态预设（environment_presets）：从已退役的一期runner原样迁出的按语言固定变量。一期命令在/root
  不可写的沙箱里运行，所以HOME与缓存都挪到/tmp。Verified的Python组别沿用它生成固定的actions.json
  （dev25结果即用此预设），不改。
- 任务环境（task_command_environment）：外部边界模式下任务容器本身就是边界，命令以容器用户（root）
  运行，挪走HOME与缓存已没有理由，反而让Harnessix比用镜像自身HOME与热缓存的mini处境更差（Go冷编译、
  看不到~/.npmrc与~/.gitconfig、Gradle找不到/root下的Wrapper发行版）。因此原样保留`docker exec`
  进程得到的容器环境（镜像ENV与其中的缓存、配置位置；不取主进程环境，镜像入口脚本会改它），HOME取
  容器默认用户的HOME，只加工具链的离线开关，并让LC_ALL跟随镜像的LANG。
  语言取Harbor任务元数据中的语言标签，不按任务或仓库特判。
本模块不依赖harnessix包，Harbor适配器（宿主机）与容器内的配置生成器共用。只服务评测，不进入产品包。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

MODEL = "qwen3.8-flash-next"
API_KEY_VARIABLE = "MODEL_API_KEY"

# ---------------------------------------------------------------- 静态预设（一期原样）

# 所有语言共用：HOME与缓存目录放到/tmp，避免写入镜像中只读或属主不同的目录。
_COMMON_ENVIRONMENT = (("HOME", "/tmp"), ("XDG_CACHE_HOME", "/tmp/.cache"))
LANGUAGE_ENVIRONMENT: dict[str, tuple[tuple[str, str], ...]] = {
    "python": (
        (
            "PATH",
            "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:"
            "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
    ),
    "go": (
        ("GOCACHE", "/tmp/go-build"),
        ("GOFLAGS", "-mod=mod"),
        ("GOMODCACHE", "/root/go/pkg/mod"),
        ("GOPROXY", "off"),
        ("GOSUMDB", "off"),
        ("GOTOOLCHAIN", "local"),
    ),
    "rust": (("CARGO_NET_OFFLINE", "true"),),
    "typescript/javascript": (
        ("COREPACK_HOME", "/root/.cache/node/corepack"),
        ("YARN_CACHE_FOLDER", "/tmp/.yarn-cache"),
        ("npm_config_cache", "/tmp/.npm"),
    ),
    "java": (
        ("GRADLE_RO_DEP_CACHE", "/root/.gradle/caches"),
        ("GRADLE_USER_HOME", "/tmp/.gradle"),
    ),
}


def environment_presets(language: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(_COMMON_ENVIRONMENT + LANGUAGE_ENVIRONMENT.get(language, ())))


# ---------------------------------------------------------------- 任务环境（外部边界）

# set：以评测值为准；default：镜像已设则沿用镜像的值；append：参数列表类变量保留镜像的参数再追加。
SwitchMode = Literal["set", "default", "append"]
_JAVASCRIPT: tuple[tuple[str, str, SwitchMode], ...] = (
    ("COREPACK_ENABLE_NETWORK", "0", "set"),
    ("YARN_ENABLE_NETWORK", "0", "set"),
    ("npm_config_offline", "true", "set"),
)
_NO_SWITCH: tuple[tuple[str, str, SwitchMode], ...] = ()
# 只含工具链认可的离线开关（均在任务镜像中核对过生效），键为Harbor任务的语言标签。
# GOFLAGS=-mod=mod沿用一期：断网时go命令只能用模块缓存补齐go.mod，镜像自带GOFLAGS时不覆盖。
OFFLINE_SWITCHES: dict[str, tuple[tuple[str, str, SwitchMode], ...]] = {
    # Verified组别用静态预设；任务环境下Python只继承镜像环境。
    "python": _NO_SWITCH,
    "go": (
        ("GOFLAGS", "-mod=mod", "default"),
        ("GOPROXY", "off", "set"),
        ("GOSUMDB", "off", "set"),
        ("GOTOOLCHAIN", "local", "set"),
    ),
    "rust": (("CARGO_NET_OFFLINE", "true", "set"),),
    "javascript": _JAVASCRIPT,
    "typescript": _JAVASCRIPT,
    # mvn与mvnw读MAVEN_ARGS（mvnd不读）；Gradle没有离线环境变量。
    "java": (("MAVEN_ARGS", "--offline", "append"),),
    "php": (("COMPOSER_DISABLE_NETWORK", "1", "set"),),
    # bundle exec不联网，Bundler没有通用的离线环境变量。
    "ruby": _NO_SWITCH,
    "c": _NO_SWITCH,
    "cpp": _NO_SWITCH,
}
# SWE-bench原版（Verified）任务全是Python，Harbor只给Multilingual任务打语言标签。
DEFAULT_LANGUAGE = "python"
# 主机名每个容器不同，不是工具链配置；模型密钥属于App Server，不给命令；
# PWD、OLDPWD、SHLVL与_是启动生成器的Shell留下的记录，不是容器环境。
_NOT_INHERITED = frozenset({"HOSTNAME", API_KEY_VARIABLE, "PWD", "OLDPWD", "SHLVL", "_"})


def task_language(tags: Iterable[str]) -> str:
    """Harbor任务标签中的语言；没有语言标签按SWE-bench原版记为Python，多个语言标签即报错。"""
    languages = sorted({tag for tag in tags if tag in OFFLINE_SWITCHES})
    if len(languages) > 1:
        raise ValueError(f"任务带有多个语言标签：{languages}")
    return languages[0] if languages else DEFAULT_LANGUAGE


def parse_environ(body: bytes) -> dict[str, str]:
    """解析/proc/<pid>/environ（NUL分隔的NAME=value）；没有等号的条目不是变量，跳过。"""
    environment: dict[str, str] = {}
    for entry in body.split(b"\0"):
        name, separator, value = entry.decode("utf-8", errors="strict").partition("=")
        if separator and name:
            environment[name] = value
    return environment


def _image_locale(container: Mapping[str, str]) -> dict[str, str]:
    """产品命令的基础环境固定LC_ALL=C，会盖过镜像只用LANG声明的区域设置（如Ruby镜像的C.UTF-8，
    否则Ruby默认编码变成US-ASCII）；镜像没有设LC_ALL时让它跟随镜像的LANG。"""
    if "LC_ALL" not in container and "LANG" in container:
        return {"LC_ALL": container["LANG"]}
    return {}


def _switched(environment: dict[str, str], language: str) -> dict[str, str]:
    for name, value, mode in OFFLINE_SWITCHES[language]:
        current = environment.get(name)
        if mode == "set" or current is None:
            environment[name] = value
        elif mode == "append" and value not in current.split():
            environment[name] = f"{current} {value}"
    return environment


def task_command_environment(
    container: Mapping[str, str],
    language: str,
    *,
    home: str,
    excluded: Iterable[str] = (),
) -> tuple[tuple[str, str], ...]:
    """容器自身环境（含缓存与配置位置）加该语言的离线开关；结果按名称排序。

    container是`docker exec`进程得到的容器环境（镜像ENV加编排声明的变量），与mini的命令和官方判分
    执行eval_script时看到的相同；excluded是适配器为智能体注入、不属于容器的变量名。HOME由调用方
    按容器默认用户给出（见harnessix_configs.container_user_home），不取生成器自身进程的HOME。
    """
    if language not in OFFLINE_SWITCHES:
        raise ValueError(f"没有该语言的离线开关表：{language}")
    dropped = _NOT_INHERITED | set(excluded)
    environment = {name: value for name, value in container.items() if name not in dropped}
    environment["HOME"] = home
    environment.update(_image_locale(container))
    return tuple(sorted(_switched(environment, language).items()))
