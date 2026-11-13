"""SKILL.md Frontmatter的安全YAML解析：3.14及以下用PyYAML，3.15起用ruamel.yaml（Beta Issue #10）。

PyYAML 6.0.3只有CPython 3.8～3.14的预编译包，Python 3.15上安装要现场编译，
Windows上没有MSVC就装不上；ruamel.yaml只发布纯Python包。两者都只做安全加载
（不构造任意Python对象）、按YAML 1.1解析、拒绝重复键；3.14及以下的行为与此前完全相同。
3.15上的已知差异（y/n、无小数点的指数、合并键覆盖、!!omap、重复锚点、%YAML 1.2指令）
都在名称、描述、版本以外的边角写法上，见ADR 0161。
"""

from __future__ import annotations

import sys
import warnings
from typing import Final

USE_PYYAML: Final = sys.version_info < (3, 15)

if sys.version_info < (3, 15):
    import yaml
    from yaml.constructor import ConstructorError
    from yaml.nodes import MappingNode
    from yaml.resolver import BaseResolver

    class _UniqueKeySafeLoader(yaml.SafeLoader):
        """拒绝重复键与不可散列键的SafeLoader。"""

    def _construct_unique_mapping(
        loader: yaml.SafeLoader, node: MappingNode, deep: bool = False
    ) -> dict[object, object]:
        loader.flatten_mapping(node)
        result: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unhashable key",
                    key_node.start_mark,
                ) from None
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found duplicate key",
                    key_node.start_mark,
                )
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    _UniqueKeySafeLoader.add_constructor(
        BaseResolver.DEFAULT_MAPPING_TAG,
        _construct_unique_mapping,
    )


class FrontmatterYamlError(ValueError):
    """Frontmatter不是安全YAML：语法错误、重复键、非安全标签等。"""


def load_frontmatter(text: str) -> object:
    """按当前Python版本选用的引擎解析一段Frontmatter。"""

    return load_with_pyyaml(text) if USE_PYYAML else load_with_ruamel(text)


def load_with_pyyaml(text: str) -> object:
    """3.14及以下：PyYAML的SafeLoader加重复键检查，与此前相同。"""

    try:
        return yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError:
        raise FrontmatterYamlError from None


def load_with_ruamel(text: str) -> object:
    """3.15起：ruamel.yaml的纯Python安全加载，按YAML 1.1解析，重复键报错。"""

    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError, YAMLWarning

    # 1.1里无小数点的指数、重复锚点等只发警告；终端界面里的警告会打乱画面。
    warnings.filterwarnings("ignore", category=YAMLWarning)
    loader = YAML(typ="safe", pure=True)
    loader.version = (1, 1)
    loader.allow_duplicate_keys = False
    try:
        return loader.load(text)
    except YAMLError:
        raise FrontmatterYamlError from None
