"""Harbor任务容器中Harnessix组别使用的本地模型标识与按语言的命令环境。

外部边界命令只得到Profile声明的变量，不继承App Server或容器的环境（ADR 0121），所以评测替每个任务
声明命令环境，有两种：
- 静态预设（environment_presets）：从已退役的一期runner迁出的按语言固定变量（Python未改动），
  Verified的Python组别沿用，生成一份固定的actions.json；
- 任务环境（task_command_environment）：任务容器自身的环境（镜像ENV，含工具链的PATH、JAVA_HOME、
  CARGO_HOME、GEM_HOME等）叠加同一套语言预设——可写缓存放到/tmp，工具链支持时强制离线。
  语言取Harbor任务元数据中的语言标签，不按任务或仓库特判。
本模块不依赖harnessix包，Harbor适配器（宿主机）与容器内的配置生成器共用。只服务评测，不进入产品包。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

MODEL = "qwen3.8-flash-next"
API_KEY_VARIABLE = "MODEL_API_KEY"

# 所有语言共用：HOME与缓存目录放到/tmp，避免写入镜像中只读或属主不同的目录。
# 镜像在/root下预备的工具链状态（模块缓存、包管理器、Maven Wrapper发行版）由各语言预设指回原处；
# 工具链支持时另加离线开关，断网时直接报离线错误，而不是逐个超时重试。
_COMMON_ENVIRONMENT = (("HOME", "/tmp"), ("XDG_CACHE_HOME", "/tmp/.cache"))
_JAVASCRIPT = (
    ("COREPACK_ENABLE_NETWORK", "0"),
    ("COREPACK_HOME", "/root/.cache/node/corepack"),
    ("YARN_CACHE_FOLDER", "/tmp/.yarn-cache"),
    ("YARN_ENABLE_NETWORK", "0"),
    ("npm_config_cache", "/tmp/.npm"),
    ("npm_config_offline", "true"),
)
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
    "javascript": _JAVASCRIPT,
    "typescript": _JAVASCRIPT,
    "java": (
        ("GRADLE_RO_DEP_CACHE", "/root/.gradle/caches"),
        ("GRADLE_USER_HOME", "/tmp/.gradle"),
        ("MAVEN_ARGS", "--offline"),
        # Maven Wrapper（mvnw）按$HOME/.m2找已下载的Maven发行版。
        ("MAVEN_USER_HOME", "/root/.m2"),
    ),
    "php": (("COMPOSER_DISABLE_NETWORK", "1"),),
    "ruby": (),
    "c": (),
    "cpp": (),
}
# SWE-bench原版（Verified）任务全是Python，Harbor只给Multilingual任务打语言标签。
DEFAULT_LANGUAGE = "python"
# 容器环境里不属于工具链配置的变量：主机名每个容器不同；HOME由共用预设覆盖。
_CONTAINER_IDENTITY = frozenset({"HOSTNAME", "HOME"})


def environment_presets(language: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(_COMMON_ENVIRONMENT + LANGUAGE_ENVIRONMENT.get(language, ())))


def task_language(tags: Iterable[str]) -> str:
    """Harbor任务标签中的语言；没有语言标签按SWE-bench原版记为Python，多个语言标签即报错。"""
    languages = sorted({tag for tag in tags if tag in LANGUAGE_ENVIRONMENT})
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


def task_command_environment(
    container: Mapping[str, str], language: str
) -> tuple[tuple[str, str], ...]:
    """容器自身环境叠加语言预设，预设优先；结果按名称排序。"""
    if language not in LANGUAGE_ENVIRONMENT:
        raise ValueError(f"没有该语言的命令环境预设：{language}")
    inherited = {
        name: value
        for name, value in container.items()
        if name not in _CONTAINER_IDENTITY and name != API_KEY_VARIABLE
    }
    merged = {**inherited, **_image_locale(container), **dict(environment_presets(language))}
    return tuple(sorted(merged.items()))
