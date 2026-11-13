"""模型输出里工具名称的长度边界：超长的名称不保留原文。"""

from __future__ import annotations

MAX_TOOL_NAME_CHARS = 256
# 超长名称在流状态里只留下这个占位：它不可能出现在工具目录里，响应结束时该次调用按目录外名称拒绝。
UNUSABLE_TOOL_NAME = ""


def usable_tool_name(name: str) -> str:
    """上限内的名称原样返回；超长的换成占位，不让模型输出的长字符串留在状态、事件或日志里。"""
    return name if len(name) <= MAX_TOOL_NAME_CHARS else UNUSABLE_TOOL_NAME
