"""对话区：定稿行追加进历史区，尚未定稿的行在下方实时区显示。"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from harnessix.product_ui.rendering import TranscriptLine

EMPTY_HINT: Final = "输入任务并按 Enter 开始；Ctrl+N 可另开新会话。"
_ROLE_STYLES: Final = {
    "你": "bold cyan",
    "Harnessix": "bold green",
    "Harnessix（生成中）": "green",
    "审批": "yellow",
    "提问": "bold yellow",
    "回答": "cyan",
    "计划": "magenta",
    "错误": "bold red",
}
# 操作过程只作背景信息，整行弱化显示。
_MUTED_ROLES: Final = frozenset({"操作", "结果", "工具调用拒绝", "进程", "上下文", "推理摘要"})


def line_renderable(line: TranscriptLine) -> RenderableType:
    """对话行的终端样式：角色着色，Agent回复按Markdown排版。"""

    if line.role == "Harnessix":
        return Group(Text(line.role, style=_ROLE_STYLES[line.role]), Markdown(line.text))
    if line.role in _MUTED_ROLES:
        return Text(f"{line.role}：{line.text}", style="dim")
    return Text.assemble((f"{line.role}：", _ROLE_STYLES.get(line.role, "bold")), line.text)


def _settled_count(lines: tuple[TranscriptLine, ...]) -> int:
    """开头连续定稿的行数；之后的行仍可能变化。"""

    return next((index for index, line in enumerate(lines) if not line.final), len(lines))


class TranscriptPane(Vertical):
    """定稿的对话只追加不重绘：长会话每次刷新全部重绘会让界面卡顿。"""

    DEFAULT_CSS = """
    TranscriptPane { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #live { height: auto; max-height: 50%; padding: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__(id="conversation")
        self._log = RichLog(id="transcript", wrap=True, markup=False, max_lines=10_000)
        self._live = Static("", id="live")
        self._history: tuple[TranscriptLine, ...] = ()
        self._thread: UUID | None = None

    def compose(self) -> ComposeResult:
        yield self._log
        yield self._live

    def show(
        self, lines: tuple[TranscriptLine, ...], thread: UUID | None, *, rewrap: bool = False
    ) -> None:
        """切换会话、需要重新折行或历史不一致时整体重绘，否则只追加新定稿的行。"""

        settled = _settled_count(lines)
        history, live = lines[:settled], lines[settled:]
        known = len(self._history)
        if rewrap or thread != self._thread or history[:known] != self._history:
            self._log.clear()
            known = 0
        for line in history[known:]:
            self._log.write(line_renderable(line))
        self._history, self._thread = history, thread
        if not lines:
            self._live.update(Text(EMPTY_HINT, style="dim"))
        else:
            self._live.update(Group(*(line_renderable(line) for line in live)))
