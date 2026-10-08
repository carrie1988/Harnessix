---
doc_type: validation-evidence
status: current
version: 1
code_revision: 5e26f952ead508dcf003c73fc54e717c6a4169d7
owners: [core]
modules: [models, agent, session, context, protocol, app_server, sdk, product_ui, product_config, evals]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/app_server/test_rejection_sdk_chain.py
  - tests/agent/test_rejection_recovery.py
  - tests/protocol/test_v1_frozen_schemas.py
supersedes: []
---

# R3类型化拒绝与公共协议升级评审包

## 接受事实

- 合法未知调用成为不可执行拒绝，结构损坏仍失败关闭；没有工具名称推测、Alias降级或模糊匹配。
- 全调用组缓冲、正常流关闭后原子提交拒绝及固定结果；已知Usage独立持久，原预算、期限和普通审批保持。
- 认证、Replay、Snapshot、Backup、Context压缩和Fork检查闭合；取消/超时/硬退出不重发旧模型、不执行拒绝。
- Provider4、Event/Thread21、Fork2、Session0031与Agent Protocol2.0配套；公共出口严格白名单，内置SDK/UI消费同一事实。
- 原十八份Schema和旧事件认证字节保持，旧1.0客户端明确版本拒绝。旧Reader负控范围只到冻结资源模拟。
- 最终同一Wheel554生产成员与源码及安装包逐字节相等。完整七目录2763通过/1原生Windows跳过；
  备份及原预算318、R4实际消费者2、其他SDK消费者81、产品CLI15通过/1原生Windows跳过分别记录，不累加重复运行。

## 拒绝扩大解释

- HTTP固定Wire、ScriptedProvider和正式组件集成均不算真实供应商、真实编码质量或Beta接受。
- 本机Windows跳过、旧资源模拟、CLI/Soak契约回归不证明Windows原生编码、历史发行可执行包降级或稳态性能。
- 两项prepared关联消费者不证明approved Writer、默认Git写工具启用、完整B4/B7或实际FD/原锁。
- 中间Wheel和31项长消费者B不替代当前最终Wheel；没有把耗时转为P1/SLA或CI成绩。
- 原R3严格0/20、必需测试1/20、真实Beta接受零保持；R3/R4及商用为OPEN/NO-GO。

## 核对入口

1. [详细设计](../../changes/m09-r3-unknown-tool-recovery.md)：目标、流程/时序/数据流、字段、伪代码、失败与升级。
2. [facts.json](facts.json)：最终候选、原件摘要、终态及旧Schema冻结集合。
3. [verification.json](verification.json)：正式安装命令、独立源码/中间边界、初败和跳过。
4. [报告](README.md)：完整读取顺序与源码链接；[manifest.json](manifest.json)：公开成员完整性。

## 下一步与Go/No-Go

可进入同一候选的真实供应商历史接受性验证，前提为准确源码/安装身份、固定Profile、完整20 Trial计划及原费用Guard准入。
真实请求费用或来源未决不能由本次离线结果核销，也不从失败试验补造Trial。
R4只继续FD/锁、B4/B7、P1和默认完整交付必需链，不扩大非阻塞首发范围。
真实质量和跨平台门禁完成之前，正式1.0发布为NO-GO。
