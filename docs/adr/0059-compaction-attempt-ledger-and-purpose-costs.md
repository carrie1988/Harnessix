---
doc_type: adr
status: current
version: 1
code_revision: b20948e1eb3a1427c13d1579e6dde94a3da0f0f6
owners:
  - core
modules:
  - context
  - agent
  - session
  - models
  - evals
related_adrs: []
related_tests:
  - tests/context
  - tests/agent
supersedes: []
---

# ADR 0059：独立摘要账本与分用途成本报告

- 状态：Accepted（账本内部切片；整体Compaction ADR 0058仍为Proposed）
- 日期：2026-09-08
- 范围：0.6.3摘要账本、费用与恢复
- 依据：[源码研究](../research/compaction-and-context-windows.md)、[详细设计](../compaction-attempt-ledger.md)

## 背景

普通ModelAttempt只在CALLING_MODEL并绑定已打开步骤；原Cost v1依赖普通(step,index)唯一性。摘要必须发生在准备模型输入阶段，目标普通步骤可能还没有打开。直接塞入原集合会混淆步骤、重试、计费与恢复；隐藏在Context函数中的HTTP请求则无法审计。

## 决策

1. 新增Turn.compactions独立集合及六种包装事件，内部复用Provider Event v3和ModelAttempt；Attempt ID跨两类集合唯一。
2. 每目标普通步骤最多一次Compaction，每Compaction最多一次Attempt。失败也消耗该次资格，首版不自动重试摘要。
3. 提取纯累计观测规则，普通和摘要复用；开始阶段、预算和归属由各自Reducer验证。普通成功步骤覆盖语义不变。
4. 开放账本期间锁定来源变更，只允许本账本推进或取消；记录绑定紧邻来源的计划sequence和终止sequence。候选通过原sequence、原已知预算计数及完整指纹重新验证，提交仍执行当前sequence CAS。
5. 保持Provider请求结算和候选接受分离。completed请求可以对应failed摘要，费用必须保留。
6. Cost v1冻结；有压缩记录时使用分用途v2，复用价格快照、Binding、固定精度估算和未知费用分类。Campaign必须对全部请求重算。
7. 统一Runtime终止收尾：请求意图已提交即视为可能发生，不自动重发；未提交候选不可从内存补造；已提交候选不是活动窗口。
8. Provider未先给出可持久请求意图而可能已经联网时，失败记录显式标记未记账请求风险，Cost v2保持不完整，禁止按零费用解释。
9. Event/Thread v14和migration16发布该内部契约。后续窗口契约另增版本，不向冻结v14追加字段。

## 取舍

独立集合增加了成本和终态守卫，但消除了将摘要冒充普通重试的歧义。同步Replay与异步在线规划共用生成器步骤，既保持取消检查，也避免复制选择算法。来源复核只在开放事件隔离下还原只读边界，不降低实际Session CAS。

当前Campaign单价格计划不能完整估算不同摘要模型，返回不完整费用是明确边界，不猜测价格。没有摘要记录的运行仍产生v1报告，避免无关数据迁移。账本可以先正式发布，但未接入HTTP消费和活动窗口之前，不标记完整Compaction生产完成。

## 验证要求

覆盖跨用途身份冲突、来源篡改、阶段及截止时间、重复累计/用量回退、成功请求但失败候选、冻结Schema、费用用途/归属/金额反例、Campaign遗漏摘要、SQLite事务切点及多次重开零请求；后续HTTP与窗口验收不由本ADR替代。
