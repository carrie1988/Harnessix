---
doc_type: adr
status: current
version: 1
code_revision: 113980facd231cc0f426830531e9e612cfac3f41
owners:
  - core
modules:
  - tools
  - agent
related_adrs: []
related_tests:
  - tests/tools
  - tests/agent
supersedes: []
---

# ADR 0053：Tool有界并发与统一错误分类

- 状态：已接受并实现
- 日期：2026-09-07
- 决策范围：0.5.6 / 0.5 Coding Tool Runtime收口

## 背景

0.5.1—0.5.5已经交付读取、搜索、Patch、Process、Git、测试、真实Eval和受控变更交付，但Tool Contract尚未表达并发资格。Kernel串行执行所有调用，降低独立仓库读取效率；如果直接并发全部调用，又会让审批或写操作越过顺序屏障。与此同时，多个工具子系统的稳定错误码被顶层分类器误归为`internal`，使Eval和运维指标不能准确区分工具失败与Runtime缺陷。

Codex与Claude Code冻结源码均表明，并发资格应由受信工具定义声明，未声明工具作为独占屏障。完整证据见[专项研究](../research/tool-scheduling-and-errors.md)。

## 决策

### D1：并发资格是默认关闭的Tool能力

`ToolDescriptor`与`ToolDefinition`新增`supports_parallel_calls: bool = false`。只有`EffectClass.READ_ONLY`可设为`true`；Descriptor构造和Registry注册均失败关闭。字段参与工具指纹，不能由模型调用参数覆盖。

### D2：Kernel只并行连续安全读取前缀

Kernel从未决调用头部选择同时满足以下条件的连续前缀：

- 调用与当前定义均为`READ_ONLY`；
- 不需要审批；
- 定义显式支持并行；
- 调用通过当前版本和完整指纹校验。

默认批次上限为4，宿主只能配置1—16。前缀不足两项走原串行路径。未声明、未知、审批、Patch、Process及任意写调用都是屏障，后续调用不能越过。

### D3：并发执行、确定性提交

安全前缀并发执行，但结果严格按Provider调用顺序校验、发布Artifact和写入Session。因此执行完成顺序不进入持久语义，同一Provider输出保持确定性Replay。

任一执行任务异常时立即取消未完成兄弟任务并等待全部任务结束；若多个任务已失败，按Provider顺序传播第一个异常。Turn协作取消和父Task取消沿用相同排空规则。

### D4：工具层再做资源上限

`CodingToolRuntime`使用默认4、范围1—16的有界信号量保护文件、搜索、Git和Artifact只读执行。`aclose`先禁止新调用，再获取全部许可并关闭Workspace，保证后台线程和目录FD不在关闭后继续使用。

该信号量只约束单个Runtime进程，不是跨进程Workspace读写锁。Patch和Process仍由各自专用端口、审批和持久恢复治理；0.7再定义Sandbox及跨进程锁。

### D5：统一工具域错误类别

保留所有稳定错误码和原优先级，只把以下前缀的未特判错误统一归为`tool`：

~~~text
tool_ patch_ process_ artifact_ test_ git_ workspace_
~~~

`process_interrupted`、审批、冲突、Provider、存储、预算和取消的既有专用分类优先，不因前缀扩展改变。

### D6：复用逐调用可观测性

每个并发调用仍经过原`tool` operation，生成独立`call_id` Span、操作计数和时延。完成先后可以从Span时序观察，但不进入Session事实。指标不新增工具名、路径、参数或并发批次ID，避免扩大高基数和源码泄漏面；顶层Turn指标使用统一后的失败类别。

## 兼容与部署

- 新字段有默认值，旧JSON/OpenAPI请求缺失时按`false`读取；
- OpenAPI只新增非必填布尔属性，不修改Action/Agent/Provider冻结版本号；
- 字段进入工具指纹，升级前未完成调用不会在新并发契约下恢复执行，而是`tool_contract_changed`失败关闭；
- 部署前应停止接收新Turn、等待在途Turn终止，再滚动升级；终态Session无需迁移；
- 无需数据库、中间件、API Key或远程服务。

## 失败语义

| 场景 | 稳定结果 |
| --- | --- |
| 并发上限不是严格整数或超出1—16 | `tool_concurrency_invalid`，Runtime不启动 |
| 写工具声明支持并行 | Descriptor校验失败，Registry不注册 |
| 调用与定义版本/指纹不一致 | `tool_contract_changed`，不执行 |
| 并发兄弟任务异常 | 取消并排空其余任务，Turn按原错误失败 |
| Turn取消 | 全部读取结束后持久化`cancelled` |
| Runtime关闭 | 拒绝新调用，等待在途读取后释放Workspace |

## 测试门禁

- Kernel并行重叠、批次上限、Provider顺序提交和串行屏障；
- 并发异常快停、兄弟任务排空和Turn取消；
- CodingToolRuntime并发上限、排队取消、关闭等待；
- Descriptor默认兼容、公开Schema和非法写注册原子失败；
- 七类工具错误前缀及既有高优先级分类回归；
- 并发调用逐项Span、计数和低基数标签回归；
- 完整质量、构建、独立wheel和远端CI。

## 后果

0.5获得了可执行而非文档性的Tool并发契约：独立读取可提高吞吐，写与审批仍保持确定性屏障，失败和取消不会遗留后台任务。当前没有跨进程Workspace锁、动态Bash并发分析或任意Shell；这些限制不得通过提高并发上限绕过。
