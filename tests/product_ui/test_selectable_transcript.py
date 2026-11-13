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

from harnessix.product_ui.rendering import TranscriptLine
from harnessix.product_ui.selectable_log import SelectableLog, highlight_characters
from harnessix.product_ui.transcript_pane import line_renderable


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


async def test_copied_lines_do_not_carry_the_padding_of_the_rendered_line() -> None:
    """对话区的Markdown回复按整行宽度补空格绘制；复制出的每行不带这些空格。

    0.10.0b30发布前在247的真实终端界面里拖选三行列表项，复制出的前两行末尾各带约80个空格。
    """
    app = _Host()
    async with app.run_test(size=(80, 12)) as pilot:
        log = app.query_one(SelectableLog)
        log.write(Text("• 第一行 alpha" + " " * 40))
        log.write(Text(" • 第二行 beta" + " " * 40))
        await pilot.pause()

        await _drag(pilot, log, (0, 0), (60, 1))
        assert app.screen.get_selected_text() == "• 第一行 alpha\n • 第二行 beta"


def test_only_the_selected_characters_are_highlighted() -> None:
    def selected(style: Style | None) -> Style:
        return (style or Style()) + Style(reverse=True)

    strip = Strip(list(Text("ab中文cd", style="green").render(Console())))
    highlighted = highlight_characters(strip, 1, 4, selected)
    pieces = [(segment.text, segment.style) for segment in highlighted if segment.text]
    assert "".join(text for text, _ in pieces) == "ab中文cd"
    assert [text for text, style in pieces if style and style.reverse] == ["b中文"]
    assert highlighted.cell_length == strip.cell_length
    # 到行尾。
    to_end = highlight_characters(strip, 4, -1, selected)
    assert [s.text for s in to_end if s.style and s.style.reverse] == ["cd"]


async def test_selected_text_stays_readable() -> None:
    """选中的字仍看得见：背景换成选择色，字色不变。

    0.10.0b30～b31直接用选择样式的Rich形式，默认主题下是“#094472 on #094472”，
    选中的几行变成一整块蓝色、字全部看不见（负责人2026-11-13截图）。
    对话区实际出现的三种行都要看：带角色色的行、弱化的操作行、按Markdown排版的回复。
    """
    shown = (
        TranscriptLine(None, "你", "读取 README.md"),
        TranscriptLine(None, "操作", "运行 pytest -q → 完成（退出码 0）"),
        TranscriptLine(None, "Harnessix", "- 撞击：白热火球 + 垂直烟羽\n- 跨度 5.62 天"),
    )
    app = _Host()
    async with app.run_test(size=(80, 12)) as pilot:
        log = app.query_one(SelectableLog)
        for line in shown:
            log.write(line_renderable(line))
        await pilot.pause()
        before = [log.render_line(y) for y in range(6)]
        await _drag(pilot, log, (0, 0), (40, 5))
        after = [log.render_line(y) for y in range(6)]

    changed = 0
    for old, new in zip(before, after, strict=True):
        old_styles = [s.style for s in old for _ in s.text]
        new_segments = [(c, s.style) for s in new for c in s.text]
        for (character, style), old_style in zip(new_segments, old_styles, strict=True):
            if character.isspace() or style is None:
                continue
            assert style.color is None or style.color != style.bgcolor, (character, style)
            if style.bgcolor != (old_style.bgcolor if old_style else None):
                changed += 1
                # 字色保持原样（角色色、弱化色）；选择只改背景。
                assert old_style is None or style.color == old_style.color, (character, style)
    assert changed > 20
