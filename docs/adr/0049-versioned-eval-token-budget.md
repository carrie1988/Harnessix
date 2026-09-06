# ADR 0049：版本化Coding Eval Token预算

- 状态：已接受；0.5.5c3a已完成离线实现，c3b真实复测已完成
- 日期：2026-09-06
- 范围：历史任务多版本Catalog、累计Token预算适用性、旧Campaign恢复和预算临界证据

## 1. 背景

首轮百炼北京真实Campaign的三个独立run均在第5个模型步骤后达到21429—21567累计Token，超过任务v1的20000上限。模型当时刚请求读取目标实现，Runtime在工具执行前停止，三个工作区均无修改。0/3因此是任务预算配置失败，不是模型修复失败。

[源码与真实数据研究](../research/eval-token-budget-applicability.md)证明，主流Coding Agent会分离当前上下文窗口/压缩阈值、单响应输出限制和累计费用或Usage预算。Harnessix不能通过改变`Budget.max_tokens`含义来掩盖Eval配置错误。

## 2. 决策

1. 保持`Budget.max_tokens`为单个Turn所有Provider已报告输入与输出Token的累计上限；
2. 保持响应后检查：下一请求输入无法在Provider权威计数前精确获知，允许记账超过上限，但超过后不得执行工具或发起下一步；
3. 保留历史任务v1及20000预算，新增任务v2并将`max_tokens`设为100000；其他任务内容和执行边界不变；
4. Catalog以`(task_id, task_version)`为身份，显式版本查询返回精确历史版本，无版本查询返回最新版本；
5. Campaign执行和恢复必须使用计划中的`task_version`，不能因安装新wheel而漂移到最新版本；
6. 不修改已完成v1 Campaign的计划、指纹、run或报告；c3b必须使用新Campaign ID、新run ID和v2指纹；
7. 预算临界可观测性复用Session、Eval报告和Campaign报告中的既有持久事实，不复制一个可能漂移的“剩余预算”字段。

## 3. 版本与兼容

| 查询 | 结果 | 任务指纹 |
|---|---|---|
| `historical_coding_eval(task_id)` | 最新v2 | `011268310f3aac1b3025d643aaf8361b807bd9d9b55a19436caa05507d122bfb` |
| `historical_coding_eval(task_id, 1)` | 原v1、20000累计Token | `ea75be4219574ff398cc252d3b5a8f870cea2f7cbb20cbe51e12e7997e921297` |
| `historical_coding_eval(task_id, 2)` | 新v2、100000累计Token | `011268310f3aac1b3025d643aaf8361b807bd9d9b55a19436caa05507d122bfb` |
| 未知任务或版本 | `eval_task_not_found` | 无 |

`historical_coding_eval_versions(task_id)`返回有序版本集合，供受信控制面列举，不接受模型决定版本。物化器、运行器、状态、报告和Campaign已有任务版本/指纹核对，无需数据库迁移或Schema升级。

旧Campaign重开时，执行器从计划读取v1并精确加载；即使当前最新版本已是v2，也不能创建v2工作区、修改旧状态或发起替代试验。新调用方若省略版本，明确选择最新v2并由其新指纹进入计划。

## 4. 预算边界与可观测性

预算检查保留两种终态：

- **恰好达到**：若响应已给出最终回答，可以完成；若响应要求工具，则停止调度工具；
- **超过上限**：持久化Provider报告的实际Usage，Turn失败为`budget_exceeded`，不得把Usage截断成上限。

Eval评分器以任务版本内预算检查`model_steps`和实际Token。报告的`metrics.input_tokens/output_tokens`继续记录事实，Campaign以`budget`主分类聚合。离线测试覆盖140/140与141/140临界值以及v1/v2 Campaign精确执行、报告和只读重开。

## 5. 为什么不是其他方案

### 修改v1预算

拒绝。任务指纹、首轮报告和可复现性会被破坏，旧Campaign无法解释。

### 把累计Token改为上下文窗口

拒绝。二者语义不同，且Harnessix 0.6 Context Engine尚未实现；这样会破坏累计消费门禁和尝试恢复。

### 仅提高到首轮观察值附近

拒绝。21567只覆盖读到目标文件之前的路径，无法覆盖后续修改、测试、Diff和最终回答，也会针对三个小样本过拟合。

### 删除Token预算，只依赖费用停止

拒绝。费用停止只在完整试验之间生效，不限制单个失控Turn；Token、步骤、时间、输出和费用是互补边界。

## 6. 失败、恢复与安全

- 未知版本在Provider创建前失败；
- 计划版本与指纹不一致在Provider创建前失败；
- 已发布计划/状态/报告继续按原子文件和摘要规则恢复；
- 提高任务预算不会提高Provider自动重试次数、并行工具能力、Shell权限或工作区写范围；
- 100000只是一项Eval累计预算，不是模型上下文窗口、供应商账户硬额度或成功率承诺；
- c3a不读取API Key、不打开网络、不使用SSH或远程中间件。

## 7. 验收与下一阶段

c3a完成条件：多版本Catalog、精确Campaign版本、预算临界回归、全量质量门禁、构建/隔离wheel和跨平台CI全部通过。

c3b已在新Campaign中使用与首轮相同的百炼北京精确模型、价格口径、Provider单尝试和三次独立run，只把任务版本改为v2。三个run均执行至第13—14个模型步骤，最终累计105435—114860 Token并以`budget`终止；完整记录见[任务v2真实基线](../validation/bailian-2026-09-06-coding-eval-v2/README.md)。

v2已关闭“读取目标实现前被20000预算截断”的原问题，但完整Session进一步证明模型在分页读取时遗漏`expected_revision`，通用`tool_invalid_arguments`无法指导纠正，连续重试导致历史和输入Token增长。该结果不改变本ADR的预算语义与版本决策；下一步不是再次提高预算，而是先为已识别的跨字段错误提供有界可纠正反馈，再以新Campaign验证。未经新授权不得追加真实试验，0.5.5d继续受有效质量基线门禁约束。
