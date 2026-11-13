"""可以用鼠标拖选、按Ctrl+C复制的对话历史区。

Textual接管了鼠标，终端自带的拖选复制不再起作用；文字选择改由Textual完成，但RichLog没有实现：
它绘制的行不带“这一格是第几个字”的位置信息，也不提供选中的文字，拖动没有任何反应。
这里补上这两样，并把选中的范围按Textual的选择样式高亮。复制由Textual的Ctrl+C完成
（终端转发剪贴板，macOS自带“终端”不支持）。
"""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog


class SelectableLog(RichLog):
    """可拖选的RichLog：选中的是各行的纯文字，不带颜色与排版标记。"""

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        # Markdown等按整行宽度补空格绘制；复制时去掉行尾空格，前面各字的位置不变。
        text = "\n".join(line.text.rstrip() for line in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        index = scroll_y + y
        strip = super().render_line(y)
        selection = self.text_selection
        if selection is not None and index < len(self.lines):
            span = selection.get_span(index)
            if span is not None:
                style = self.screen.get_component_rich_style("screen--selection")
                strip = highlight_characters(strip, span[0] - scroll_x, span[1], style)
        # 位置信息让Textual把鼠标落点换算成第几行第几个字。
        return strip.apply_offsets(scroll_x, index)


def highlight_characters(strip: Strip, start: int, end: int, style: Style) -> Strip:
    """把一行里第start到end个字（end为-1表示到行尾）叠加上选择样式，其余原样。"""

    segments: list[Segment] = []
    position = 0
    for text, segment_style, control in strip:
        following = position + len(text)
        first = max(start - position, 0)
        last = len(text) if end == -1 else min(end - position, len(text))
        if control or first >= last:
            segments.append(Segment(text, segment_style, control))
        else:
            combined = (segment_style or Style()) + style
            pieces = (
                (text[:first], segment_style),
                (text[first:last], combined),
                (text[last:], segment_style),
            )
            segments.extend(Segment(piece, piece_style) for piece, piece_style in pieces if piece)
        position = following
    return Strip(segments, strip.cell_length)
