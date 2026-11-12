"""服务端默认思考的云端模型及其传输等待时间。

百炼qwen3.8系列默认推送思考内容（2026-11-10实测flash与max），产品写文件工具的参数要整段生成完才
一次发出；代码较长时流中静默常超过30秒空闲读取，被当成连接中断。0.10.0b8起向导为它们写入思考型
限额；更早向导写下的旧默认值（空闲读取30秒、单请求120秒）在运行时按新值理解，用户升级后不必
重新配置。用户改过的值原样使用；输出上限等其他参数不变，计费与上下文预算不受影响。
"""

from __future__ import annotations

from urllib.parse import urlsplit

LEGACY_DEFAULT_TIMEOUTS = (30.0, 120.0)
REASONING_TIMEOUTS = (180.0, 900.0)


def reasons_by_default(base_url: str, model: str) -> bool:
    """已求证在服务端默认思考的云端模型：没有声明思考协议的配置不加思考参数，等待时间须按思考放宽。

    0.10.0b19起向导为百炼qwen3.8-max与qwen3.8-flash预设思考协议；此前生成的配置和同系列其他型号
    仍没有协议，继续按这里的名单放宽等待时间与输出限额。
    """

    host = urlsplit(base_url).hostname
    # 智谱GLM-5.3系列强制思考、不能关闭（智谱文档，2026-11-10）。
    return (host == "dashscope.aliyuncs.com" and model.startswith("qwen3.8")) or (
        host == "open.bigmodel.cn" and model.startswith("glm-5.3")
    )


def effective_timeouts(
    base_url: str, model: str, io_timeout_seconds: float, timeout_seconds: float
) -> tuple[float, float]:
    """（空闲读取，单请求）实际使用的等待时间：只把旧向导默认值升级为思考型等待时间。"""

    if (
        reasons_by_default(base_url, model)
        and (
            float(io_timeout_seconds),
            float(timeout_seconds),
        )
        == LEGACY_DEFAULT_TIMEOUTS
    ):
        return REASONING_TIMEOUTS
    return io_timeout_seconds, timeout_seconds
