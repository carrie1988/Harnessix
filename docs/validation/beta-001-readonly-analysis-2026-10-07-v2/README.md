---
doc_type: validation-evidence
status: current
version: 1
code_revision: 3fb2ef57f3a647b20058f987f090e6829c4d8a58
owners: [core]
modules: [sdk, product_config, agent, evals]
related_adrs:
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_coding_workflow_instructions.py
  - tests/product_config/test_agent_context.py
  - tests/tools/test_search_kernel.py
  - tests/tools/test_search_boundaries.py
supersedes: []
---

# BETA-001 导航指令同条件真实复验

## 1. 结论

保持[首次尝试](../beta-001-readonly-analysis-2026-10-07-v1/README.md)的12文件输入、Prompt、Provider及四步/16,000Token/180秒预算，
以新候选和独立安装发起一个新Turn。四条真实请求均有完整费用估算结算，但Turn仍以`budget_exceeded`失败。
原认证历史确认四次工具仍是成功的`list_files`：`.`、`backend`、`backend/src`、`backend/src/main`。
没有`read_file`调用或源码读取成功，没有观察到真实导航改善，不计源码分析完成、登录整改或Beta PASS。

[共享导航指令v5](../../changes/m09-r3-bounded-source-navigation.md)的发布和离线工具能力通过，不能强制模型遵循。
原两次失败均保留；不再同条件重复发送，也不通过扩大原限制改写原失败。

## 2. 执行身份与边界

| 项目 | 事实 |
|---|---|
| 候选 | `3fb2ef57f3a647b20058f987f090e6829c4d8a58` |
| Wheel | `1.0.0rc1`，SHA256 `751b3a67ba6978e4729a4b531b01b7bd3d7ddb7805559419b0dca838e277a584` |
| 一致性 | 548件包成员、507件Python源码；候选/Wheel/非可编辑独立安装字节一致 |
| 指令 | `harnessix.coding-instructions/v5`，2747 UTF-8字节；原2751护栏保持 |
| 宿主 | 新状态目录及实际请求身份；原真实嵌入式SDK/协议/认证Session/WorkspaceScope复用 |
| 输入 | 同一12文件、50,712字节；无参考整改代码或答案，前后摘要和文件身份一致 |
| 权限 | 仅五个只读工具；无Patch、测试进程、MCP、Hook或Skill |
| 重开 | 原失败Thread/Turn重开及分页Replay一致；不是取消、备份恢复或默认CLI/TUI验收 |

配置准备阶段曾因复制宿主的精确根尚未重绑定及非Schema配置字段而离线拒绝，均未创建运行状态、未发模型、未开账本。
依据原类型契约修正后，同一最终宿主离线装配通过；这些私有准备错误不隐去，也不归因于产品权限或模型。

## 3. 实际用量与费用

本次输入13,693、输出448、共14,141Token；直接失败是四步用尽，不是16,000Token耗尽。
本次费用估算`0.06194`元。共享60元周期累计8条请求全部`completed`、已知用量估算`0.134792`元、预留`0`、无新增unknown。
观察时剩余估算`59.865208`元；供应商账单金额仍为null，不用0。旧两unknown原件及其预留保留，不计入新额度、不阻塞新路径。
R3与Beta仍共用唯一账本，新unknown停止规则不变。

## 4. 验证与证据

源码64-case回归、独立40-case工具/Context/取消/越界/预算负对照通过；二者都为离线验证，不能推出自主模型效果。
实际SDK路径和源码追踪见[首次运行资料](../beta-001-readonly-analysis-2026-10-07-v1/README.md#5-证据与源码追踪)。
原保护历史只读事务未initialize原库、未新增模型或打开费用Owner；数据库及保护收据摘要保持。
不承诺全部SQLite协调侧车物理字节不变，也不宣称原业务项目全部正文完整未变。

公开交付为[facts.json](facts.json)、[review-packet.json](review-packet.json)、[manifest.json](manifest.json)和本报告。
受控证据根E的`beta-analysis-v2/network-delivery-v1/`包含完整Markdown、结构化结论、原运行收据、认证诊断、
实际起止时间与退出码、费用快照、候选来源、Review Packet及逐件SHA256。个人路径、凭据和业务源码不公开。

## 5. 后继计划与发布门禁

后继正式只读分析应明确经审阅的允许相对路径清单及适当的新任务预算；清单仅为输入与权限元数据，不包含代码答案或修复建议。
该新任务与本次同条件验证分开，不据新路径信息或更大预算宣称v5因果改善，不改产品默认预算、原Turn、Task Pack或评分器。

完整业务副本、原目录全内容快照、整改审批、取消、隔离业务测试、浏览器验证及使用者验收仍未完成，BETA-001完成数0。
R3质量、三平台消费者、正式Git Writer、独立Beta与正式1.0发布门禁仍开放。
