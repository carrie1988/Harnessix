---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [tools, context]
related_adrs:
  - docs/adr/0145-whole-repository-search-on-large-repositories.md
  - docs/adr/0129-file-tool-leniency-and-correctable-rejections.md
  - docs/adr/0162-grep-returns-matches-found-before-the-time-limit.md
related_tests:
  - tests/tools/test_glob_prefix.py
  - tests/tools/test_regex_grep_runtime.py
  - tests/context/test_tool_result_view.py
  - tests/artifacts/test_model_history.py
supersedes: []
---

# ADR 0165 Glob与Grep同一组整仓库上限，只进入可能有命中的目录

- 状态：评审中
- 日期：2026-11-15
- 修订：快照与正则搜索模式下的Glob输入、输出与执行合同（`bounded-search/v11`）

## 背景

GitHub Issue #35（0.10.0b46，Windows）：在Prettier仓库根目录调用`glob`，`pattern="tests/format/**/class*/**/*.ts"`，
3次都以`tool_limit_exceeded`结束，一条结果也没有；把`path`设为`tests/format/typescript`后扫描1,675个条目、0.2秒返回40条。
超限说明还建议“先用glob找候选文件”，而失败的正是glob。

本机用同一提交（2cc4f0fd）的Prettier检出、真实工具Runtime（产品默认的正则搜索模式）复现：ADR 0129的参数修复已把开头
的固定目录移到`path`（从`tests/format`开始找`**/class*/**/*.ts`），但`tests/format`下就有11,237个条目，仍超过Glob的
10,000个条目上限。ADR 0145把快照与正则Grep的上限放宽到整仓库（200,000个条目、32 MiB名称），Glob沿用原字面量合同的
上限，中型仓库的一个子目录就能让Glob失败。macOS、Linux与Windows的遍历与上限相同，三个平台都受影响。

## 决策

1. **整仓库Glob。** 启用快照或正则Grep时（产品默认），`glob`绑定换成`WholeRepositoryGlobInput`/`WholeRepositoryGlobOutput`：
   条目与名称上限与快照Grep相同（`MAX_SNAPSHOT_SEARCH_ENTRIES`、`MAX_SNAPSHOT_SEARCH_NAMES_BYTES`），统计为`SnapshotSearchStats`；
   输入字段与语义不变。Glob只看文件名、不读内容，字节上限与读取时限照旧约束总开销。
2. **只进入可能有命中的目录。** 上限放宽二十倍后，遍历成本不应随仓库其余部分增长：`PathPattern.may_contain`
   与`matches`用同一张逐段状态表，判断相对起点的目录下是否可能有匹配；不可能时不进入该子树
   （`search._collect`与`windows_search._collect`的`descend`判定，两个平台共用`search.glob_walk`）。
   ADR 0129的参数修复只在起点为工作区根、开头是固定目录时生效；这里还覆盖`packages/*/src/**/*.ts`、根目录的`*.ts`
   与非根起点。结果与按原规则遍历相同（只跳过不可能命中的目录）。
3. **合同。** 执行合同`bounded-search/v11`写明`glob.max_entries`、`glob.max_names_bytes`与
   `walk: only-directories-that-can-contain-a-match`；新增`spec/whole-repository-glob-*-v1`。存档输出为
   `ArchivedWholeRepositoryGlobOutput`；Context的工具结果视图按整仓库Glob输出识别`paths`预览（统计可超过10,000）。
4. **超限说明。** Glob超限时不再建议“先用glob”，改为缩小`path`或把`pattern`开头的通配段换成预期的目录名
   （`read_error_messages.GLOB_LIMIT_HINT`）；Grep的说明不变。

## 没有改变的

- 未启用快照或正则Grep时的原Glob：遍历、上限、输出合同与Descriptor逐字节不变。
- Glob的模式语义、排序、结果与输出上限、超时行为。

## 后果

- Prettier上的同一调用0.4秒返回40条（扫描11,236个条目，只遍历`tests/format`）；此前扫描上限失败。
- 产品默认模式下`glob`的Descriptor摘要改变（新绑定），工具调用按新指纹进行。

## 验证

- `tests/tools/test_glob_prefix.py`：`may_contain`与`matches`同规则（含大小写、`**`结尾）；POSIX与Windows（替身根）上带固定前缀
  的整仓库Glob只遍历该子树、`**/*.ts`仍找到全部33个；原Glob不剪枝、输出仍为`GlobOutput`；把原上限压到20时原Glob超限、
  整仓库Glob完整返回（修改前两个平台都失败）；Glob超限说明不再建议先用glob，Grep说明不变。
- `tests/tools/test_regex_grep_runtime.py`：原Descriptor不变；快照与正则模式共用新的Glob Descriptor。
- `tests/context/test_tool_result_view.py`、`tests/artifacts/test_model_history.py`：统计超过10,000的整仓库Glob预览按`paths`省略；
  存档链路在两种模式下相同。
- 真实工具Runtime（macOS，Prettier 2cc4f0fd，正则搜索模式）：报告人的参数修改前`tool_limit_exceeded`，修改后0.4秒返回40条。
