---
doc_type: validation-evidence
status: current
version: 1
code_revision: 4b643f13fc54ae70050a9507ec29bf884ac9eda4
owners: [core]
modules: [tools, processes, workspace, product_config]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_windows_git.py
  - tests/tools/test_git_platform_contracts.py
  - tests/processes/test_windows_owner_lifecycle.py
  - tests/processes/test_windows_supervisor.py
  - tests/product_config/test_server_and_cli.py
supersedes: []
---

# Windows Git与Owner生命周期 Review Packet

## 固定对象和判定

源码`4b643f13fc54ae70050a9507ec29bf884ac9eda4`，报告[README](README.md)，
总体与详设[Windows Git读取](../../changes/m09-r4-windows-native-git-read.md)。
**GO**：合入功能候选并继续原恢复主线；**NO-GO**：宣称Windows完整产品验收通过、关闭R4或发布1.0。

## 源码复核

1. [`tools/git.py`](../../../src/harnessix/tools/git.py)：检查原Catalog绑定、固定三查询、配置键名捕获及Filter/Include拒绝。
2. [`workspace/git_windows_binding.py`](../../../src/harnessix/workspace/git_windows_binding.py)：复用原链，拒绝Reparse及多链接EXE。
3. [`git_read_windows.py`](../../../src/harnessix/processes/git_read_windows.py)：完整Plan先落盘、Shield排空、同目录事实、重复UNKNOWN拒绝和16件上限。
4. [`windows_owner.py`](../../../src/harnessix/processes/windows_owner.py)：读线程成功前后关闭归属唯一，终态不等待Controller刷新。
5. [`windows_job.py`](../../../src/harnessix/processes/windows_job.py)：原Handle归属核验在Resume前，失败禁止恢复。
6. [`server.py`](../../../src/harnessix/product_config/server.py)：正式SDK装配与Root Owner顺序；不得将Root普通创建误作严格Windows私有权限证明。
7. [`state_backup_files.py`](../../../src/harnessix/product_config/state_backup_files.py)：完整备份保留严格验权，没有以放宽ACL掩盖原生失败。

## 证据复核

- 核对Manifest列出的实际文件集合、大小与SHA-256，不能只验证已列出的部分文件。
- 核对三轮源码、Job ID与日志结论，不相加Revision通过数，不把中断或取消记作PASS。
- 检查有界诊断原栈的读线程`:186`与主线程`:422`，与修复前源码对应。
- 检查`4b643f1`冷Receipt用例PASS、三个退出码用例PASS、四类取消/超时PASS。
- 检查唯一失败发生于默认SDK用例的备份阶段，34通过/5跳过/1失败结论保留。
- 检查两套Python环境源码相同，各1084通过/50跳过；Windows跳过不得替代原生证据。
- 检查图源、真实PNG、字段、源码及测试一致，不将目标R1权限架构绘制为已实现。
- 检查Wheel只含正式生产包，版本仍0.1.0，未含未知测试目录或Secret；不是1.0发布物。

## 开放功能风险

| 风险 | 处置 | 禁止的捷径 |
|---|---|---|
| Windows默认Root与完整备份权限合同不一致 | 下一R1切片优先统一创建/读取和实际恢复 | 放宽现行私有验证器、只修Root或删除测试 |
| R3真实任务结果未验收 | 原冻结Task Pack与预算内真实请求验证 | 以离线ScriptedProvider替代质量 |
| R4消费者安装/升级仍开放 | 固定发行物三平台独立验证 | 以源码检出下pytest代替安装 |
| 完整CI未全部结束 | 保留快照与后续独立观察 | 提前把进行中Job计作成功 |

发行材料优先级低于核心功能与恢复；该优先级不等于正式分发条件被取消。
