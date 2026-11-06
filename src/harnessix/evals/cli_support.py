"""评测CLI共用脱敏参数诊断；不加载业务配置或网络凭据。"""

import argparse
from typing import NoReturn


class SafeEvalParser(argparse.ArgumentParser):
    """参数出错时仅输出固定用法提示，不回显操作者传入的内容。"""

    def error(self, message: str) -> NoReturn:
        """保持argparse退出码2，丢弃可能含凭据的底层诊断。"""
        del message
        self.exit(2, f"{self.prog} 参数无效；使用 --help 查看格式。\n")
