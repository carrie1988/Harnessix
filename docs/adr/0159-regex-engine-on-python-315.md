---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [tools, governance]
related_adrs:
  - docs/adr/0145-whole-repository-search-on-large-repositories.md
related_tests:
  - tests/tools/test_regex_grep_py315.py
  - tests/tools/test_regex_grep.py
  - tests/tools/test_regex_grep_runtime.py
  - tests/governance/test_sbom.py
supersedes: []
---

# ADR 0159 Python 3.15起正则搜索改用regex引擎

- 状态：评审中
- 日期：2026-11-13
- 修订：正则搜索“RE2缺失或漂移时失败关闭，不回退到回溯引擎”的约定（`tools/grep_matcher.py`）；许可证策略v2的允许清单

## 背景

正则搜索只用google-re2：它是线性时间引擎，模型写出的病态表达式不会让搜索卡住，代码里明确“不回退到其他引擎”。
google-re2的最新版本（1.1.20251105）只有CPython 3.10～3.14的预编译包，三个平台都没有3.15的。Python 3.15已于
2026年10月发布，uv在没有指定版本时会选它：安装转为源码编译，没有C++工具链（Windows上要求MSVC，POSIX上还需要abseil）时失败。

- 0.10.0b25 Windows Beta复测（GitHub Issue #10）：`uv tool install harnessix`选中3.15，安装失败；0.10.0b27起文档的安装命令都写
  `--python 3.12`作为绕行。0.10.0b32复测（Issue #10的评论）：指定3.15仍失败，指定3.12正常。
- 在依赖里声明`requires-python <3.15`无效：3.15为默认解释器时，uv会退回到没有这条声明的旧版本，同样失败。

产品负责人2026-11-13决定：3.15上换引擎，选用`regex`库，并在许可证策略里允许它的`CNRI-Python`条款（方案C1）；
法律评估暂不考虑，作为未决事项记录。

## 决策

1. **按Python版本只装一个引擎。** `google-re2==1.1.20251105; python_version < '3.15'`照旧；
   `regex==2026.11.9; python_version >= '3.15'`。3.14及以下的行为、执行合同与工具描述逐字不变。
   开发依赖组另含`regex`，使3.12～3.14上也能测试3.15的路径。
2. **3.15上的匹配（`grep_matcher._regex_line_matcher`）：**
   - 先按RE2的语法拒绝回溯引擎才有的写法（`supported_by_re2`）：反向引用（`\1`、`\g`、`\k`、`(?P=…)`）、`\K`、
     前后查找、原子组、占有量词、递归、条件分支、注释组、RE2没有的内联标志（如`x`、`a`），以及超过1000的计数重复。
     逐字符扫描，跳过转义与字符类内部。拒绝与编译失败都是`tool_regex_invalid`，说明与RE2相同，不回显表达式。
   - 以`regex.VERSION0`编译，区分大小写。不加ASCII标志：它会让`\pL`等Unicode类失效；因此3.15上`\d\w\s\b`按Unicode判定，
     与RE2的ASCII语义在非ASCII字符上不同，这是已知差异。
   - 每行匹配限时0.25秒；超时以`tool_regex_timeout`结束整次搜索，建议简化表达式或改用`match_mode=literal`。
     整次搜索仍受原读取时限约束。
   - `regex`缺失或版本不符时`tool_regex_unavailable`，失败关闭，不再退到第三种引擎；字面量搜索不受影响。
3. **执行合同按引擎给出**（`execution_contract`）：3.15上`engine`为`regex`，并写明限时与不支持的写法；
   它进入工具目录的规则摘要，所以3.15与3.14上的工具指纹不同，这是预期的。
4. **许可证策略v2的允许清单加入`CNRI-Python`**（`governance/license-policy-v2.json`），`regex`声明
   `Apache-2.0 AND CNRI-Python`，策略要求AND的每一项都获准。SBOM（78个包、912个锁定Archive）、许可证证据与扫描结果重新生成。

## 没有改变的

- Python 3.14及以下：仍只用google-re2，失败关闭，不回退。
- 字面量搜索、快照搜索的扫描与统计口径、整份文本不命中时跳过逐行扫描的规则（ADR 0145）。

## 后果

- Python 3.15上可以直接`uv tool install harnessix`；三个平台相同（版本条件与平台无关）。
- 3.15上的安全保证由“引擎本身线性时间”变为“语法限制加每行限时”：病态表达式最多让一行耗费0.25秒，随后整次搜索结束。
- **未决：** `CNRI-Python`许可证与AGPL-3.0的兼容性未经法律评估（自由软件基金会认为CNRI Python许可证与GPL不兼容）。
  负责人决定先发布、法律问题另行处理；`regex`只在3.15及以上安装。

## 验证

- `tests/tools/test_regex_grep_py315.py`：在当前解释器上切到3.15的路径，经真实工具Runtime：与RE2相同的匹配行、
  两个引擎在同一组表达式与行上的首个命中位置逐一相同、十类RE2不接受的写法被拒且不回显、超时以`tool_regex_timeout`结束、
  缺失或版本漂移时失败关闭而字面量照常、执行合同写明引擎、每个Python版本只声明一个引擎且版本与合同一致、语法检查表。
- 原RE2测试在没有google-re2的环境里整文件跳过（3.15）。
- 三个平台用Python 3.15实际安装（`uv tool install --python 3.15`）并运行正则搜索，由发布会话执行。
