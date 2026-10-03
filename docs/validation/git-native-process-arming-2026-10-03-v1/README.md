---
doc_type: validation-evidence
status: current
version: 1
code_revision: dc1953acc92e201714a3fa9a8bc6ad3032b40a2b
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/governance/test_windows_git_native_process_arming.py
supersedes: []
---

# Windows Git 创建事件布防限定验证

基准为`dc1953acc92e201714a3fa9a8bc6ad3032b40a2b`。仅修改既有观察器bootstrap的事件入口，完整方案见[详细设计](../../changes/m09-r4-git-native-process-arming.md)。不是生产Git材料修复、Windows支持验收或完整Git交付。

- 新8项回归在原字节下1失败/7通过，修复后与原227项共同235通过，0失败/错误/跳过。
- 原三个handler生成字节一致，仅bootstrap删除`sxi cpr`并将`ld:git.exe`替换为`cpr:git.exe`；完整原16件输入和期限不变。
- 格式串单反斜线换行正确，本变更没有修改printf或日志解析器。
- 图已渲染并逐图检查；文档、Secret与原字节核对结果见[verification.json](verification.json)，源码定位见[SOURCE.md](SOURCE.md)。
- 原Run37077033494及其他FAIL不覆盖、不重跑。新候选真实原生结果须另行记录，发布本包时未宣告CDB或SDK成功，历史根因仍UNKNOWN。

复现入口见[COMMANDS.md](COMMANDS.md)，限定评审见[review.json](review.json)，精确发布集合见[manifest.json](manifest.json)。
