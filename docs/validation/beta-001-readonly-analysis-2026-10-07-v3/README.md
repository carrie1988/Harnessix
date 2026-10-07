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
  - tests/agent/test_usage_observations.py
supersedes: []
---

# BETA-001 新计划只读分析与语义拒绝

## 1. 结论与适用边界

结果为 `HOLD_TOKEN_AND_SEMANTIC_REVIEW`。新计划发生了真实源码读取，但 Turn 失败，模型末段文字也未通过语义审阅。
没有执行登录整改、批准 Patch、构建、业务回归或浏览器验收；真实 Beta 完成任务数仍为0。
[同条件v5失败](../beta-001-readonly-analysis-2026-10-07-v2/README.md)和[初次失败](../beta-001-readonly-analysis-2026-10-07-v1/README.md)均原样保留。
新 Prompt 提供经审阅的12件输入路径，并独立设定16步/100,000 Token/180秒边界，不修改原失败的预算或结果。
路径提示属于本次任务输入变化，不把读取改善归因于共享指令v5，不计为原R3评分通过。

## 2. 固定输入、安装件和正式执行链

- 候选 `3fb2ef57f3a647b20058f987f090e6829c4d8a58`；非可编辑独立安装 `1.0.0rc1`。
- Wheel SHA256：`751b3a67ba6978e4729a4b531b01b7bd3d7ddb7805559419b0dca838e277a584`。
- 548件包成员、507件Python源码；候选/Wheel/安装源字节一致。
- 输入仍为12件已审阅初始源码、50,712字节；没有参考整改代码、补丁或答案。前后内容摘要及文件身份相同。
- 使用 `AgentClient → InProcessAgentTransport → AgentProtocolServer → AgentApplicationService → AgentRuntime`。
- 仅五个既有只读工具；不装配 Patch、Process、Hook、MCP、Skill，不连接业务服务。
- 原Thread重开及96条公开事件分页Replay一致，扫描窗口3、最终游标162；不是取消或备份恢复验收。

## 3. 实际模型及工具事实

固定百炼模型 `qwen3-coder-plus-2025-09-23`，北京端点；每次最多2048输出Token、单尝试、零重试。
实际11个模型步骤、11次请求均有完整用量并结算为 `completed`；工具调用10次 `read_file`，9成功、1失败。
成功结果覆盖8件独立源码，其中一件分页两次；没有 `list_files` 调用。
分页漏传 `expected_revision` 被既有工具以 `tool_expected_revision_required` 拒绝，后继携带原Revision读取成功，未放宽参数合同。
这与 Provider 请求自动重试不同，不把正常的原工具纠错隐藏为零失败。

原认证历史确认累计输入101,641、输出2,185、共103,826 Token。
Turn `max_tokens` 是累计各次模型请求用量，包括重复发送的上下文，不是单请求上下文窗口大小。
第11次请求已有完整用量后超过100,000，原Runtime以 `budget_exceeded` 拒绝完成；不是16步或180秒耗尽。
末段2331字符模型文字已持久化，但不能因其存在而将失败Turn改为成功。

## 4. 独立语义审阅

| 观察 | 判定与理由 |
|---|---|
| 声称HTTP客户端未读源于权限限制 | 拒绝；该路径已经位于12件许可输入，实际没有调用，不存在该工具的权限拒绝 |
| 认证核心链路缺少实际读取 | 拒绝完整分析；HTTP客户端、前端认证状态及后端认证Service均未进入成功读取证据 |
| 使用隐含HTTPS描述传输 | 无已验证部署证据，不接受为当前事实；浏览器Network与Console也未实际复现 |
| 末段声称完整分析 | 拒绝；关键链路覆盖不足且Turn失败，必要接口、登录约束和测试范围未完整求证 |

语义拒绝与累计Token拒绝为两项独立结论；仅扩大预算不能自动消除语义缺口。
后继任务必须要求关键链路实际读取、结论绑定准确源码行号，并独立冻结新任务计划。
不修改产品默认预算、评分分母、原失败、Provider身份或权限来消除失败记录。

## 5. 费用与受保护状态

本次估算0.441524元；三次共19请求，本轮累计估算0.576316元、预留0、没有新增unknown。
60元是R3/Beta共用额度，不是各自60元；旧两笔未决费用未计入，也未删除或结算为零。
金额按完整Provider用量及公开价格估算，供应商实际账单仍未确认。

诊断使用原Product Owner、原Scope、原Session Key及 `authenticated_thread_history`。
不调用Session初始化、Runtime、Provider或预算Owner，不绕过MAC，不导出原ToolResult源码或凭据。
受保护主库、原Key材料、运行收据、宿主、源码清单及新旧账本摘要未变；共享内存侧文件的完全不变未作承诺。
该观察不能外推原业务目录所有正文、长期不变、默认CLI/TUI或完整恢复。

## 6. 代码映射和后续门禁

- [任务登记与业务边界](../../operations/pilot-tasks/001-login-password-protection.md)、[单人先导手册](../../operations/pilot-beta.md)。
- [共享导航指令详设](../../changes/m09-r3-bounded-source-navigation.md)、[Context装配](../../../src/harnessix/product_config/agent_context.py)。
- [原Runtime预算结算](../../../src/harnessix/agent/runtime.py)、[已报用量契约](../../../src/harnessix/agent/usage.py)。
- [原认证历史入口](../../../src/harnessix/session/sqlite.py)、[原只读工具边界](../../../src/harnessix/tools/runtime.py)。
- [60元周期与历史保留](../../changes/m09-provider-budget-period-activation.md)、[事实清单](facts.json)、[评审包](REVIEW_PACKET.md)。

完整业务输入、隔离环境、整改审批、最终测试、取消/恢复、人工验收、R3与三平台商用门槛仍独立开放。
公共包不包含客户源码、完整Prompt、模型文字正文、密钥、个人绝对路径或原始SQL事件；原件在受控交付目录封存。
