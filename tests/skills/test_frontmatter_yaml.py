"""SKILL.md Frontmatter在Python 3.15起改用ruamel.yaml（Beta Issue #10）。

PyYAML 6.0.3没有Python 3.15的预编译包，Windows上没有MSVC就装不上Harnessix。
3.15起改用纯Python的ruamel.yaml，按YAML 1.1安全加载、拒绝重复键。常见写法两个引擎
结果相同；已知的边角差异在这里固定下来。在3.12～3.14上两个引擎都可用（开发依赖），逐项对比。
"""

from __future__ import annotations

import tomllib
import warnings
from importlib import metadata
from pathlib import Path

import pytest

from harnessix.skills import SkillSource, frontmatter_yaml
from harnessix.skills.frontmatter_yaml import (
    FrontmatterYamlError,
    load_with_pyyaml,
    load_with_ruamel,
)
from tests.skills.test_runtime import registry

ROOT = Path(__file__).resolve().parents[2]
BOTH_ENGINES = pytest.mark.skipif(
    not frontmatter_yaml.USE_PYYAML, reason="PyYAML只在3.14及以下安装"
)

COMMON = {
    "plain": "name: review\ndescription: 审查代码\nversion: 1.0.0\n",
    "integer_version": "name: a\ndescription: b\nversion: 2\n",
    "float_version": "name: a\ndescription: b\nversion: 1.2\n",
    "booleans": "a: true\nb: False\nc: yes\nd: no\ne: on\nf: off\n",
    "nulls": "a: ~\nb: null\nc:\n",
    "numbers": "a: 010\nb: 0x1F\nc: 1_000\nd: 1:30\ne: -.inf\nf: 6.8e+5\n",
    "quoted": "a: 'it''s'\nb: \"tab\\there\"\nc: \"\\u4e2d\"\nd: '010'\ne: 'yes'\n",
    "block_scalars": "description: >\n  第一行\n  第二行\nbody: |\n  a\n  b\n",
    "lists_and_maps": "allowed-tools:\n  - Read\n  - Bash(git:*)\nflow: [a, b]\nmeta: {x: 1}\n",
    "anchors": "a: &x [1, 2]\nb: *x\n",
    "merge": "base: &b\n  x: 1\nderived:\n  <<: *b\n  z: 2\n",
    "unicode": "name: 中文技能\ndescription: 描述：带冒号\n",
    "crlf_and_bom": "\ufeffname: a\r\ndescription: b\r\n",
    "explicit_tags": "a: !!str 10\nb: !!int '10'\nc: !!float 1\n",
    "scalar_document": "just text\n",
    "empty": "",
}

REJECTED = {
    "duplicate_key": "name: review\nname: hijacked\n",
    "nested_duplicate": "m:\n  x: 1\n  x: 2\n",
    "python_tag": "a: !!python/object:os.system x\n",
    "unknown_tag": "a: !foo bar\n",
    "unclosed_flow": "a: [1, 2\n",
    "bad_indent": "a:\n  b: 1\n c: 2\n",
    "two_documents": "a: 1\n---\nb: 2\n",
    "tab_indent": "a:\tb\n",
    "colon_in_plain_value": "description: use when: x\n",
}


@BOTH_ENGINES
@pytest.mark.parametrize("text", COMMON.values(), ids=COMMON.keys())
def test_common_frontmatter_reads_the_same_with_both_engines(text: str) -> None:
    assert repr(load_with_ruamel(text)) == repr(load_with_pyyaml(text))


@pytest.mark.parametrize("text", REJECTED.values(), ids=REJECTED.keys())
def test_unsafe_or_invalid_frontmatter_is_rejected(text: str) -> None:
    with pytest.raises(FrontmatterYamlError):
        load_with_ruamel(text)
    if frontmatter_yaml.USE_PYYAML:
        with pytest.raises(FrontmatterYamlError):
            load_with_pyyaml(text)


def test_known_differences_on_python_315_stay_in_corner_cases() -> None:
    """ADR 0161列出的差异；变化时这里先失败，再决定是否更新文档。"""

    assert load_with_ruamel("a: y\nb: n\n") == {"a": True, "b": False}  # PyYAML：'y'、'n'
    assert load_with_ruamel("a: 1e3\n") == {"a": 1000.0}  # PyYAML：'1e3'
    assert load_with_ruamel("a: &x 1\nb: &x 2\n") == {"a": 1, "b": 2}  # PyYAML：重复锚点报错
    assert load_with_ruamel("%YAML 1.2\n---\na: yes\n") == {"a": "yes"}  # PyYAML：True


def test_ruamel_warnings_never_reach_the_terminal() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_with_ruamel("a: 1e3\nb: &x 1\nc: &x 2\n")
    assert caught == []


def test_skills_load_on_python_315_and_keep_their_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(frontmatter_yaml, "USE_PYYAML", False)
    source = tmp_path / "source"
    documents = {
        "review": "---\nname: review\ndescription: >\n  审查\n  代码\nversion: 2\n---\n正文\n",
        "duplicate": "---\nname: a\nname: b\ndescription: c\n---\n正文\n",
        "large": "---\nname: large\ndescription: d\na: &a [x, y]\nb: ["
        + ",".join("*a" for _ in range(256))
        + "]\n---\n正文\n",
    }
    for directory, document in documents.items():
        (source / directory).mkdir(parents=True)
        (source / directory / "SKILL.md").write_text(document, encoding="utf-8")
    runtime, _ = registry(tmp_path, SkillSource("workspace", "workspace", source))

    catalog = runtime.discover()

    skills = [(skill.name, skill.description, skill.declared_version) for skill in catalog.skills]
    assert skills == [("review", "审查 代码", "2")]
    assert sorted(issue.code for issue in catalog.issues) == [
        "skill_frontmatter_invalid",
        "skill_frontmatter_limit",
    ]


def test_each_python_version_installs_exactly_one_yaml_engine() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = {item.split(">")[0].split("=")[0]: item for item in project["dependencies"]}
    assert dependencies["pyyaml"] == "pyyaml>=6,<7; python_version < '3.15'"
    assert dependencies["ruamel.yaml"] == "ruamel.yaml==0.19.1; python_version >= '3.15'"
    assert metadata.version("ruamel.yaml") == "0.19.1"
