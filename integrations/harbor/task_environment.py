"""Harbor任务容器中Harnessix组别使用的本地模型标识与按语言的命令环境预设。

从已退役的一期runner（scripts/swebench_local_run.py）原样迁出：同一本地模型名、同一密钥变量名、
同一组按语言的固定环境变量（不含秘密）。只服务评测配置生成，不进入产品包。
"""

from __future__ import annotations

MODEL = "qwen3.8-flash-next"
API_KEY_VARIABLE = "MODEL_API_KEY"

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
