---
doc_type: adr
status: reviewing
version: 1
code_revision: a33e5ace26b50ccd6aa24fada64fefc07764dbc1
owners: [core]
modules: [context, product-config]
related_adrs:
  - docs/adr/0132-append-only-requests-and-token-calibrated-compaction.md
  - docs/adr/0134-default-working-habits-and-thin-eval-template.md
related_tests:
  - tests/context/test_turn_stable_source.py
supersedes: []
---

# ADR 0147 工作区概览在Turn内固定，系统指令不在中途变化

- 状态：评审中
- 日期：2026-11-12
- 修订：Context模块设计中“`WorkspaceContextSource`每Step双读目录Revision”一条——产品装配里概览改为每个Turn的
  首个模型步骤观察一次

## 1. 背景

ADR 0132把宿主说明写进历史，使每个请求都是“上一请求加新内容”，服务端的前缀缓存可以一直复用。第二轮候选版本
在开发集（30题×2轮）上仍有约5%的模型调用要等15秒以上，合计占模型时间的13%。逐项排查后，排除了历史被改写、
转发层更换副本和失败重试；剩下的原因在请求最前面的系统指令里：

- 系统指令由若干片段组成，其中`workspace/layout.json`是工作区根目录与工作目录的一级条目概览，**每个模型步骤重新观察**。
- 模型在根目录新建或删除一个条目（临时脚本、构建目录、测试文件），概览正文就变，系统指令随之变化。
- 系统指令排在请求最前面，它一变，服务端从这里起的前缀缓存全部失效，整段历史要重算。

60个试次的会话记录里，系统指令在Turn中途变化26次，分布在13个试次，**全部来自这一片段**。这26步的等待中位数
是44.6秒（其余步骤5.2秒），每千Token上下文1.46秒，与完整重算的速度一致；上下文越长代价越大（最长146秒）。
同一副本上的另一道题在这段时间里几乎停住，所以实际代价约为这26步自身等待的数倍。

这不是自托管服务独有的问题：按量计费并提供前缀缓存的服务上，同样的变化是一次全价输入。

## 2. 决策

新增[`TurnStableContextSource`](../../src/harnessix/context/turn_stable_source.py)：包装一个Context来源，
同一Thread的同一Turn内只在首个模型步骤观察，其后各步沿用该次观察；下一Turn重新观察。
产品装配（`build_product_agent_context`）用它包装`WorkspaceContextSource`。

- 模型已经从自己的工具结果里知道它对根目录做了什么，概览只用于开局定位，Turn内不刷新不丢信息。
- 用户在两次输入之间改动了工作区，下一Turn的概览照常反映。
- 项目指令来源与环境来源不包装：前者在Turn中途变化应当立即生效，后者不随步骤变化。
- 包装只在内存中按Thread记住最近一次观察，最多32个Thread；进程重启后首个步骤重新观察，最坏是一次缓存未命中。
- 不改变来源的观察方式、正文、片段身份的算法，也不改变Context检查记录的形状。

## 3. 没有采用的做法

- **把概览移出系统指令、放进历史。** 需要改Context规划与片段信任级别的契约，范围大。
- **只在条目变化不大时沿用。** 任何变化都会使缓存失效，没有“变化不大”的中间状态。
- **按Thread固定（跨Turn也不刷新）。** 用户在两次输入之间的改动会看不到。

## 4. 后果与边界

- 同一Turn内的各个请求，系统指令逐字节相同（项目指令文件被修改时除外）。
- 组合观测的一致性检查（同一步内两次观察须一致）对被包装的来源恒成立；它原本防的是观察期间的并发变化，
  固定之后不存在这种变化。工作区根在Turn中途被整个替换时，其余来源的范围摘要会与固定的概览不一致，
  该步以`context_sources_changed`失败，与此前根被替换时的行为相同。
- `preparing_context`里每步两次的目录列举随之省去。

## 5. 验证

- [`tests/context/test_turn_stable_source.py`](../../tests/context/test_turn_stable_source.py)：同一Turn内根目录变化后
  观察与产品系统指令不变，下一Turn刷新；Thread之间不共用，记忆有界。
- 评测中的核对方法：会话记录里`context_prepared`的`instruction_fingerprint`在一个Turn内应只有一个取值；
  等待超过15秒的模型调用占比应明显下降。
