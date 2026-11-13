"""能粘贴多行文本的单行输入框（Beta Issue #19）。

Textual的单行Input收到粘贴时只插入第一行，其余静默丢弃：把Issue标题和正文整段粘贴进来，只剩标题。
这里把多行粘贴显示为一个标记（例如“[粘贴的文本 #1：12 行]”），提交时还原成粘贴的原文，换行保留；
单行粘贴照旧直接插入。删掉标记就等于不要这段粘贴。三个平台的终端都是同一行为。
"""

from __future__ import annotations

from textual import events
from textual.widgets import Input


class PasteAwareInput(Input):
    """多行粘贴以标记显示、提交时还原原文；读取提交内容用full_value，提交后调用clear_pastes。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._pastes: dict[str, str] = {}

    @property
    def full_value(self) -> str:
        """当前内容，其中仍在的粘贴标记换回粘贴的原文。"""

        value = self.value
        for token, text in self._pastes.items():
            value = value.replace(token, text)
        return value

    def clear_pastes(self) -> None:
        self._pastes.clear()

    def _on_paste(self, event: events.Paste) -> None:
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in text.strip("\n"):
            return  # 单行：交给Input自己的处理（Textual随后调用基类的同名处理器）。
        lines = len(text.strip("\n").split("\n"))
        token = f"[粘贴的文本 #{len(self._pastes) + 1}：{lines} 行]"
        self._pastes[token] = text
        if self.selection.is_empty:
            self.insert_text_at_cursor(token)
        else:
            self.replace(token, *self.selection)
        # 基类的处理器只插入第一行，不能再执行。
        event.prevent_default()
        event.stop()


def submitted_text(event: Input.Submitted) -> str:
    """提交的完整内容：多行粘贴的标记还原成原文。"""

    widget = event.input
    return widget.full_value if isinstance(widget, PasteAwareInput) else event.value


def clear_input(widget: Input) -> None:
    """提交后清空输入框，连同记下的粘贴原文。"""

    widget.value = ""
    if isinstance(widget, PasteAwareInput):
        widget.clear_pastes()
