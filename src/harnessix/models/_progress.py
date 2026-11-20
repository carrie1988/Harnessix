"""工具调用参数的生成进度：适配器按累计UTF-8字节数上报，只有数字和工具名，没有内容。

模型整段生成写文件的参数时，流里几十秒到几分钟没有正文也没有思考，界面看起来像卡住。
适配器每收到一批参数片段就上报一次累计字节数；第一批立即上报，之后每满一段才再报，避免事件过密。
工具调用开始、参数还是空的时候先报一次0字节与工具名：有的服务把数组、对象参数写完才整段发出
（实测vLLM、百炼的Qwen工具解析；Anthropic按完整键值发出），界面据此立即切到“正在生成修改内容”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harnessix.models.contracts import ProviderEvent, ToolArgumentsProgress

PROGRESS_STEP_BYTES: Final = 256


@dataclass(slots=True)
class ArgumentProgress:
    """一次响应里全部工具调用参数的累计UTF-8字节数与已上报的位置。"""

    received: int = 0
    reported: int = 0
    announced: bool = False

    def add(self, fragment: str, tool: str | None) -> list[ProviderEvent]:
        size = len(fragment.encode("utf-8", "replace"))
        self.received += size
        if not size:
            if self.received or self.announced:
                return []
            self.announced = True
            return [ToolArgumentsProgress(utf8_bytes=0, tool=tool)]
        due = self.received - self.reported >= PROGRESS_STEP_BYTES or not self.reported
        if not due:
            return []
        self.reported = self.received
        return [ToolArgumentsProgress(utf8_bytes=self.received, tool=tool)]
