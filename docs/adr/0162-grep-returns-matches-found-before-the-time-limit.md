---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [tools]
related_adrs:
  - docs/adr/0160-read-operations-verify-the-root-once.md
  - docs/adr/0145-whole-repository-search-on-large-repositories.md
related_tests:
  - tests/tools/test_grep_time_limit.py
supersedes: []
---

# ADR 0162 读取时限到了而已有命中时，Grep返回这些命中

- 状态：评审中
- 日期：2026-11-14
- 修订：快照与正则Grep的输出合同（`truncation_reason`增加`time_limit`）与执行合同（v9、v10）

## 背景

GitHub Issue #21、#24：Windows上在大仓库根目录整仓grep超过5秒读取时限，一条结果也没有。ADR 0160第1～4版把热缓存下
真实Dify检出（16,108个文件）的整仓grep降到约1.7秒，但刚克隆的仓库第一次搜索仍会超时：发布会话在Windows 11真机上
对照，PowerShell的`Select-String`第一次搜同一份检出要13.3秒、第二次0.76秒；Harnessix的剖析里`CreateFileW`占了冷缓存
时间的约70%。第一次读文件内容要先经Defender扫描，任何搜索工具都要付这笔时间。Issue #24的报告人希望“若无法完成，
保留已找到的匹配，或提示下一步如何缩小搜索范围”。超时提示原本就建议缩小path、收窄include；保留已找到的匹配要改
输出合同，负责人2026-11-14同意实施（“按你推荐的实施”）。

## 决策

1. **快照与正则Grep（产品默认）**：读取时限到了、而这次已经找到至少一条命中（含字面量回退的命中）时，不再以
   `tool_timeout`失败，而是返回已找到的命中：`truncated=true`、`truncation_reason="time_limit"`、`scan_complete=false`；
   有Artifact存档时，存档同样标为不完整。没有命中时照旧以`tool_timeout`失败（没有可用的结果）。
2. **只放宽这一种输出**：`GrepSnapshotOutput`（正则Grep共用）的`truncation_reason`增加`time_limit`；原字面量Grep与Glob的
   输出合同、行为逐字节不变。执行合同由`bounded-search/v7`（快照）、`v8`（正则）改为`v9`、`v10`，并写明
   `on_timeout: return-matches-found-so-far-as-time_limit`。
3. **工具说明**写明：读取时限内没搜完时返回已找到的命中，`truncation_reason`为`time_limit`，其余文件没有搜过，需要完整
   结果时缩小path或收窄include再搜。
4. **两个平台同一规则**：`search.timed_out_with_results`判定，`search.grep`（POSIX）与`windows_search.grep_files`在遍历与
   读取外层接住超时；判定与结果组装（`grep_result(..., timed_out=True)`）共用。已经读到的每条命中都来自一次完整、稳定
   的文件观察，与正常结果相同。

## 没有改变的

- 5秒读取时限、扫描上限、命中与上下文的合同。
- 收集阶段（遍历目录）就超时的搜索仍然没有命中可返回，照旧失败。

## 后果

- 刚克隆的大仓库第一次搜索，模型能先拿到已经找到的命中继续工作，再按需缩小范围补搜。
- 结果不完整由`truncation_reason`明确标出；模型不能把它当作“仓库里只有这些”。

## 验证

- `tests/tools/test_grep_time_limit.py`：POSIX与Windows（替身根）各自，快照与正则Grep在第一条命中之后超时，返回这条命中、
  标为`time_limit`、存档不完整；没有命中、或原字面量Grep，照旧超时失败。修改前四种组合都以超时失败。
- 真实工具Runtime（macOS，正则Grep，读取时限1秒，16,000个文件的合成仓库）：1.0秒返回32条命中、`time_limit`、
  `files_read`约9,000～9,700，此前同一调用以`tool_timeout`失败。
