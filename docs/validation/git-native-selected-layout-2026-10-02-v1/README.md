---
doc_type: validation-evidence
status: current
version: 1
code_revision: 110185d9d2d24bb138f7ca0941dac5b32873f918
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
  - tests/governance/test_windows_git_native_selected_layout.py
supersedes: []
---

# Windows Git 选中 launcher 最小固定材料适配验证包

## 文档摘要

基线110185；唯一既有源码修改为preflight.py的selected_paths。root/bin仅作为wrapper候选，wrapper必须引用实际selected本人；原cmd/core、固定PE/PDB、源合同、13hooks、两SDK selector、期限及拒绝策略保持。不是现场Pythonwhich或原生断点证明。

## 内容与身份

- [完整详设](../../changes/m09-r4-git-native-selected-layout.md)：背景、选型、角色、接口、字段、异常、预算、持久化边界及部署回退。
- [SOURCE.json](SOURCE.json)：当前源码和原函数保持、旧输入身份、index不写。
- [facts.json](facts.json)：checkout、固定上游安装映射、MinGit材料、实际现场未知项分列。
- [verification.json](verification.json)：实际153与项目外官方材料13，通过和原RED分开。
- [COMMANDS.json](COMMANDS.json)、[review-packet.json](review-packet.json)：有限离线复验及固定revision原生运行条件。
- [originals/preflight.py](originals/preflight.py)：原c08aa精确字节，历史快照，不作为当前入口。
- [selected-layout.mmd](selected-layout.mmd)、[selected-layout.png](selected-layout.png)：当前角色分支实际渲染/查看；原四图只继承原验证状态。
- [manifest.json](manifest.json)：写集、固定输入及继承图的完整字节身份，排除自引用。

## 实际验证与历史保留

当前正式153 PASS＝原132＋新21；新21复用既有合成PE/PDB解析fixture，不作为官方材料。项目外新快照官方13 PASS，实际check_pair/debugger生成/recheck未替代，七例原程序正文保持，另六项漂移/不偷换cmd验证。仅隔离宿主平台、源码及工具准备/符号下载，不执行进程。

修复前本地1 FAIL RED和独立官方6 PASS/1 FAIL RED保留。初次fixture输出含空格及缺预算字段的失败只列为宿主测试配置历史。早先132＋10分析结果、原v2及输入接合历史结果不累计当前153/13。

## 范围与限制

已有checkout bin/version和固定runner安装映射不等于Python实际which。缓存MinGit无bin成员，现场bin/cmd是否同byte未测；适配只允许实际selected通过原fixedpair，不能据目录、版本或上游main放行。2.56/UCRT64支持没有扩大。

[Run82实际拒绝](../git-native-layout-refusal-2026-10-02-v1/README.md)保持非执行FAIL，原Git128根因UNKNOWN，presence null不推断工具缺失。旧公开资料和原测试不改；旧清单对当前preflight的预期差异单列。没有Windows/CDB/SDK执行、dispatch、PATH/Git/SDK替换、网络、模型、凭据、账本、Docker、stage或push；不是W1、全仓、R3或商用验收。
