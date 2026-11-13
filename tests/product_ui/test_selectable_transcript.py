"""对话历史区可以用鼠标拖选、按Ctrl+C复制（负责人2026-11-13的反馈）。

Textual接管了鼠标，终端自带的拖选复制不再起作用；对话区原来用的RichLog不支持Textual的文字选择，
拖动没有任何反应。
"""

from __future__ import annotations

from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.strip import Strip

from harnessix.product_ui.selectable_log import SelectableLog, highlight_characters


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield SelectableLog(id="transcript", wrap=True, markup=False)


LINES = (
    "你：读取 README.md",
    "Harnessix：第一行是 # demo",
    "操作：运行 pytest -q → 完成（退出码 0）",
)


async def _drag(pilot, log: SelectableLog, start: tuple[int, int], end: tuple[int, int]) -> None:  # type: ignore[no-untyped-def]
    await pilot.mouse_down(log, offset=start)
    await pilot.hover(log, offset=end)
    await pilot.mouse_up(log, offset=end)
    await pilot.pause()


async def test_dragging_across_the_history_selects_its_plain_text_and_ctrl_c_copies_it() -> None:
    app = _Host()
    async with app.run_test(size=(80, 12)) as pilot:
        log = app.query_one(SelectableLog)
        for line in LINES:
            log.write(Text(line, style="bold cyan"))
        await pilot.pause()

        # 从第一行开头拖到第二行第10格（“：”）：松开处的字也算选中。
        await _drag(pilot, log, (0, 0), (9, 1))
        selected = app.screen.get_selected_text()
        assert selected == "你：读取 README.md\nHarnessix："

        await pilot.press("ctrl+c")
        assert app.clipboard == selected


async def test_selecting_inside_one_line_and_the_whole_history() -> None:
    app = _Host()
    async with app.run_test(size=(80, 12)) as pilot:
        log = app.query_one(SelectableLog)
        for line in LINES:
            log.write(line)
        await pilot.pause()

        # 第三行：“操作：运行”五个字各占两格，“pytest”从第11格开始。
        await _drag(pilot, log, (11, 2), (16, 2))
        assert app.screen.get_selected_text() == "pytest"
        await _drag(pilot, log, (6, 2), (8, 2))
        assert app.screen.get_selected_text() == "运行"
        await _drag(pilot, log, (0, 0), (40, 2))
        assert app.screen.get_selected_text() == "\n".join(LINES)


def test_only_the_selected_characters_are_highlighted() -> None:
    selected = Style(reverse=True)
    strip = Strip(list(Text("ab中文cd", style="green").render(Console())))
    highlighted = highlight_characters(strip, 1, 4, selected)
    pieces = [(segment.text, segment.style) for segment in highlighted if segment.text]
    assert "".join(text for text, _ in pieces) == "ab中文cd"
    assert [text for text, style in pieces if style and style.reverse] == ["b中文"]
    assert highlighted.cell_length == strip.cell_length
    # 到行尾。
    to_end = highlight_characters(strip, 4, -1, selected)
    assert [s.text for s in to_end if s.style and s.style.reverse] == ["cd"]
