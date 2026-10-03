---
doc_type: validation-evidence
status: current
version: 1
code_revision: 9b9e52fdaab74567b5ad7cd5614801f1936689bc
owners: [core]
modules: [delivery, product_config, governance]
related_adrs:
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/product_config/test_git_object_material.py
  - tests/governance/test_windows_git_native_failure_projection.py
  - tests/governance/test_windows_git_trace2_input_binding.py
supersedes: []
---

# 精确静态目录候选的固定Windows原生结果

## 1. 结论

固定候选9b9e52f的[Run37103867851](https://github.com/carrie1988/Harnessix/actions/runs/37103867851)，
attempt1实际终态failure，未重跑、未超时。安装锁定依赖及结果上传成功，执行步骤失败。
18件完整输入和官方PE/PDB配对通过，但两个原SDK Case均Git128/Worker2、proof ABSENT、SDKfalse、RootUNKNOWN。
新三个静态ID均未观测，不能把静态目录缺口认定为该现场根因；原新增目录保留其离线契约覆盖价值。

两Case仍为profile MATCHED、completeness UNKNOWN、UNCLASSIFIED_FORMAT及MATCHED_128；
ENTRY_START_MATCHED、DISPATCH_HASH_OBJECT、REPO_EVENT_SEEN存在，已知集合仅HASH_OBJECT_ADD_AGGREGATE。
CDB arm/branch标记均0，不能用Trace2阶段信号替代原独立分支见证。
完整有限事实见[facts.json](facts.json)及[原result.json](result.json)。

## 2. 输入、证据与表示

源码/合同及离线覆盖见[944项限定验证](../windows-trace2-static-formats-2026-10-03-v1/README.md)，
正式设计与未完成边界见[详细设计第12节](../../changes/m09-r4-windows-trace2-role-input-binding.md#12-精确静态错误目录与失败发布接合)。
仅下载原workflow上传的result.json与result-sha256.json，不读取CDB、raw、stderr或原业务正文日志。
原result 5547字节、SHA256为`2ee304b2534c3725e3e65bcdfb42548a0068e187b45294f7b11d4a4f0e5a1be8`，
与[result摘要记录](result-sha256.json)一致；公开result保持原字节。
摘要记录采用规范LF JSON保存原字段值，不声称其排版字节等同Windows原件。
公开manifest绑定实际Git LF表示，不以原CRLF摘要误标公开文件。

## 3. 失败、安全及后继

旧两次现场失败、旧冻结包和本新失败全部保留，不重跑覆盖、不自动扩大诊断或原成功条件。
有限结果仅UNAUTHENTICATED_DIAGNOSTIC_ONLY；Root、caller、errno、实际输入proof和业务原因继续未知。
后继需从实际受控输入、Windows对象操作及原进程见证求证；只有新修复和对应原生结果才能关闭故障。
本结果没有修改审批、期限、能力、13 hook、原九件guard或完整Git业务范围，没有新增模型请求或费用。
R1～R6、完整Git/Backup v2、R3真实质量、消费者Windows和独立Beta继续开放。
