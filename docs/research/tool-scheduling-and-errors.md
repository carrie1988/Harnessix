---
doc_type: source-research
status: historical
version: 1
code_revision: 113980facd231cc0f426830531e9e612cfac3f41
owners:
  - core
modules:
  - tools
  - agent
related_adrs:
  - docs/adr/0053-tool-concurrency-and-error-taxonomy.md
related_tests:
  - tests/tools
  - tests/agent
supersedes: []
---

> **冻结源码研究**：本资料的参考版本与访问日期冻结于2026-09-07；具体提交、版本和证据位置见正文及[统一研究基线](baselines.md)。结论不随上游分支移动自动更新，Harnessix现行行为以关联ADR和模块设计为准。

# Tool 调度与错误分类专项研究

- 研究日期：2026-09-07
- 范围：同一模型响应内的多 Tool 调度、取消和错误归类
- 结论用途：0.5.6 Tool Contract 收口

## 1. 冻结源码基线

| 项目 | 本地提交 |
| --- | --- |
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` |
| Claude Code 逆向仓库 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` |

这里只记录可复核的本地源码事实和由事实导出的 Harnessix 决策，不复制实现代码，也不把提示词建议当作强制执行边界。

## 2. 源码事实

### 2.1 Codex：Registry 能力声明与读写门

- `codex-rs/core/src/tools/router.rs:235-239`从 Registry 查询单个调用是否支持并行，未知或未声明时返回`false`；
- `codex-rs/core/src/tools/registry.rs:486-489`只允许可见工具通过自身 Runtime 声明并行能力；
- `codex-rs/core/src/tools/parallel.rs:115-178`使用`RwLock`作为调用门：支持并行的工具持有读锁，其余工具持有写锁，因此未声明工具成为前后调用的独占屏障；
- `codex-rs/core/src/tools/parallel.rs:180-208`在取消时终止未到终态的调度任务，并生成明确的中止结果；
- `codex-rs/core/src/tools/handlers/mcp.rs:128-139`只在服务端显式允许或MCP工具具有只读提示时允许并行。

结论：并发资格是受信 Registry/Runtime 属性，不是模型参数；默认值必须保守，非并发调用必须与并发调用互斥。

### 2.2 Claude Code：连续安全批次与串行屏障

- `src/services/tools/toolOrchestration.ts:19-81`将调用划分为并发安全批次和串行批次；
- `src/services/tools/toolOrchestration.ts:84-116`只合并连续的并发安全调用，输入解析失败或能力判断抛错时按不安全处理；
- `src/services/tools/toolOrchestration.ts:152-170`并发执行安全批次；
- `src/services/tools/StreamingToolExecutor.ts:104-150`在流式路径再次计算能力，只有当前执行项全部安全时才准入新的安全调用；非安全调用保持顺序；
- FileRead、Glob、Grep显式声明并发安全，Bash根据参数动态判断，而不是按工具名称无条件并行。

结论：连续前缀批处理能够保持模型给出的屏障顺序；解析或能力判断异常必须关闭并发，而不是乐观执行。

### 2.3 OpenCode：异步 Tool 回调和多调用状态

- `packages/opencode/src/session/llm.ts:276-324`把异步 Tool Definitions交给AI SDK `streamText`执行；
- `packages/opencode/src/session/tools.ts:398-489`为每个MCP工具包装独立异步执行、权限、Hook、输出裁剪和调用身份；
- `packages/opencode/src/session/processor.ts:330-419`独立跟踪每个Tool Call的running/result/error状态；
- `packages/opencode/src/session/processor.ts:585-607`清理时并发等待所有未决调用，超时后将剩余调用明确标记为中断；
- 多份内置模型提示要求无依赖调用并行，但在本次冻结源码范围内没有发现统一的Effect Class与读共享/写独占调度门。

结论：OpenCode证明模型运行层需要容纳多个在途调用，但提示词不能承担写互斥。Harnessix不采用“所有异步回调均可并行”的隐式策略。

## 3. Harnessix 差距

0.5.5结束时已有严格`ToolDescriptor`、Tool Call指纹、审批、取消和持久结果，但仍有三个缺口：

1. Tool Contract没有可执行的并发能力字段；
2. Kernel逐个执行同一响应的调用，无法并行独立读取；
3. Patch、Process、Artifact、Git、Workspace等工具域错误未统一归入`tool`类别，部分错误会错误显示为`internal`。

任意Shell、跨进程Workspace锁和容器级隔离属于不同问题，不能用本次进程内调度冒充。

## 4. 采用的契约

### 4.1 能力声明

`ToolDescriptor.supports_parallel_calls`为受信、默认`false`的可选字段。只有`READ_ONLY`工具可以声明`true`；Registry在写入内部映射前构造并校验Descriptor，非法写工具注册失败且不污染注册表。

该字段参与完整Tool指纹。升级后已有终态历史仍可读取；升级前未完成调用因指纹不同而`tool_contract_changed`失败关闭，不在新并发语义下重放。

### 4.2 Kernel 调度

同一模型响应按Provider顺序处理：

1. 从未决调用开头选取连续、无需审批、调用/定义均为`READ_ONLY`且定义显式opt-in的前缀；
2. 最多并行4项，可由宿主在1—16内收紧；
3. 前缀不足两项时沿用原串行路径；
4. 任意未声明工具、审批、Patch、Process或写调用都是屏障，屏障后的调用不越过它；
5. 执行结果按Provider原顺序校验和持久化，完成先后不改变Session事件顺序；
6. 任一并发任务异常时立即取消并排空未完成兄弟任务，再按Provider顺序选择首个异常结束Turn；
7. Turn取消和父Task取消都必须排空全部子任务。

每个调用继续使用既有`harnessix.agent.tool` Span与`harnessix.agent.operations`/duration指标。并发调用拥有独立`call_id` Span；指标标签仍只含固定operation/outcome/category，不增加工具名、路径或参数等高基数/敏感标签。

`CodingToolRuntime`对文件、搜索和Artifact读取再使用1—16的有界信号量，默认4，避免外部调用绕过Kernel后形成无界线程/FD占用。关闭Runtime时先禁止新调用，再等待全部许可回收后关闭Workspace。

### 4.3 错误分类

`tool_`、`patch_`、`process_`、`artifact_`、`test_`、`git_`和`workspace_`前缀统一归入`FailureCategory.TOOL`。更具体的高优先级语义保持不变：

- `process_interrupted`仍为`interrupted`；
- 审批类仍为`approval`；
- `runtime_busy`等CAS/占用冲突仍为`conflict`；
- Provider、存储、预算和取消仍使用原分类。

本次只统一顶层类别，不改稳定错误码、`retryable`、持久事件Schema或模型可见消息。

## 5. 未采用方案

- **所有只读Effect自动并行**：拒绝。第三方Runtime可能没有线程安全保证，必须显式opt-in；
- **一次性`gather`全部调用**：拒绝。会让写调用越过顺序屏障并形成无界并发；
- **按完成顺序持久结果**：拒绝。会使相同Provider输出产生不确定Replay顺序；
- **兄弟任务自然结束后再上报失败**：拒绝。阻塞读取可能让失败Turn永久等待；
- **把进程内并发称为Workspace全局锁**：拒绝。跨进程/跨宿主互斥必须由0.7的锁与Sandbox契约解决。

## 6. 验收要求

- 两个opt-in读取真实重叠，三个读取受Kernel与工具层上限约束；
- 结果按Provider顺序持久化；
- 未opt-in读取、审批和写调用保持屏障；
- 单项异常立即取消并排空兄弟任务；
- Turn取消、Runtime关闭、非法上下限和非法写能力均有回归；
- 并发读取仍生成逐调用Span和低基数操作指标；
- 旧Descriptor缺失新字段可读取为`false`，公开OpenAPI重新生成且生成确定；
- 完整Ruff、Mypy、测试、异步严格模式、构建、独立wheel和远端CI通过。
