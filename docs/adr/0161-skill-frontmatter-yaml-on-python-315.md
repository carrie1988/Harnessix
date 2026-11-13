---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [skills, governance]
related_adrs:
  - docs/adr/0159-regex-engine-on-python-315.md
  - docs/adr/0074-skill-snapshot-and-hook-action-boundary.md
related_tests:
  - tests/skills/test_frontmatter_yaml.py
  - tests/skills/test_runtime.py
  - tests/governance/test_sbom.py
supersedes: []
---

# ADR 0161 Python 3.15起Skill Frontmatter改用ruamel.yaml

- 状态：评审中
- 日期：2026-11-13
- 修订：Skill Frontmatter的YAML加载器（[`skills/frontmatter_yaml.py`](../../src/harnessix/skills/frontmatter_yaml.py)）；运行依赖

## 背景

[ADR 0159](0159-regex-engine-on-python-315.md)让Python 3.15不再需要google-re2之后，发布会话在0.10.0b35候选（uv 0.13.7，
Python 3.15.2）上实测安装：

- macOS、Linux能装，但PyYAML 6.0.3是现场从源码编译的（本机有clang或gcc）；
- Windows真机仍然失败：`Microsoft Visual C++ 14.0 or greater is required`，`pyyaml (v6.0.3) was included because harnessix depends on pyyaml`。

PyYAML 6.0.3只有CPython 3.8～3.14的预编译包，三个平台都没有3.15的；普通Windows用户没有MSVC。对六个平台（Windows x64/arm64、
macOS x64/arm64、manylinux x64/arm64）做Python 3.15、只用预编译包（`--only-binary :all:`）的解析：去掉PyYAML后其余依赖都能
解析，选到的版本与默认解析相同，没有“最新版缺预编译包、退回源码”的情形。PyYAML只用于解析SKILL.md的Frontmatter。

## 决策

与ADR 0159相同，按Python版本只装一个引擎，3.14及以下完全不变（负责人对Issue #10的决定：3.15上换引擎）。

1. **依赖。** `pyyaml>=6,<7; python_version < '3.15'`；`ruamel.yaml==0.19.1; python_version >= '3.15'`。ruamel.yaml只发布
   纯Python包（`py3-none-any`），C扩展在可选的extras里，许可证MIT（策略已允许）。开发依赖两者都装：在3.12～3.14上也能测试
   3.15的路径，文档门禁与治理测试照旧用PyYAML。
2. **加载器。** `frontmatter_yaml.load_frontmatter`按`USE_PYYAML`（`sys.version_info < (3, 15)`）选引擎：
   - 3.14及以下：原来的`_UniqueKeySafeLoader`（SafeLoader加重复键与不可散列键检查），从`skills/runtime.py`原样移入；
   - 3.15起：`YAML(typ="safe", pure=True)`，按YAML 1.1解析，`allow_duplicate_keys=False`；
   - 两者的错误都映射为`FrontmatterYamlError`，`SkillRegistry`照旧报`skill_frontmatter_invalid`；结构上限、名称、描述、
     版本的检查不变。
3. **警告。** ruamel.yaml对1.1里无小数点的指数、重复锚点只发`YAMLWarning`；终端界面里打印警告会打乱画面，加载时忽略这一类别
   （只限ruamel.yaml自己的警告类别）。

## 3.15上的已知差异

常见写法（文本、引号、块标量、列表、映射、布尔`true/yes/on`、整数`010/0x1F/1_000/1:30`、浮点、空值、锚点与别名、合并键、
显式标签、BOM与CRLF）两个引擎结果相同；重复键、Python标签、未知标签、语法错误、多文档都拒绝。以下边角写法不同，
都不在名称、描述、版本的常见写法里：

| 写法 | PyYAML（3.14及以下） | ruamel.yaml（3.15起） |
|---|---|---|
| `y`、`n`作为值 | 文本 | 布尔 |
| `1e3`（无小数点的指数） | 文本 | 浮点 |
| 同名锚点重复定义 | 报错 | 后者生效 |
| `<<`合并后又写同名键 | 报错（重复键） | 显式键覆盖 |
| `!!omap` | 元组列表，被结构检查拒绝 | 有序映射，通过 |
| `%YAML 1.2`指令 | 仍按1.1 | 按1.2（`yes`为文本） |

差异由`tests/skills/test_frontmatter_yaml.py::test_known_differences_on_python_315_stay_in_corner_cases`固定；ruamel.yaml
升级时先在这里失败。

## 没有改变的

- 3.14及以下的依赖、加载器与全部行为。
- Skill的上限、Manifest语义与错误码。

## 后果

- Python 3.15上三个平台都只装预编译包，不再需要C编译器。
- 运行依赖多一个包（79个包、914个锁定Archive）；SBOM、许可证证据与扫描结果重新生成。
- 同一份SKILL.md在3.15与更早的版本上，只在上表的边角写法里结果不同。

## 验证

- `tests/skills/test_frontmatter_yaml.py`：16类常见写法两个引擎逐项相同；9类非法写法两个引擎都拒绝；已知差异；不输出警告；
  3.15路径下经真实`SkillRegistry`发现Skill、拒绝重复键、别名展开仍受上限约束；依赖声明与已安装版本。
- `tests/skills`、`tests/governance`在macOS（Python 3.12）上通过。
- 发布会话：三个平台用`uv tool install --python 3.15`实际安装（Windows真机没有MSVC），运行带Skill的任务；3.12安装照旧。
