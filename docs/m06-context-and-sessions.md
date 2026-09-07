# 0.6 Context Engine 与持久会话详细实施设计

- 更新日期：2026-09-07
- 状态：0.6.1 已完成实现，等待本提交远端CI；整体0.6进行中
- 目标：支持长任务、多轮会话和可解释、可恢复的上下文管理

## 1. 实施顺序

0.6继续采用可发布纵向切片，每片均包含正式契约、失败语义、持久化、可观测性、测试和文档。

| 切片 | 内容 | 状态 |
|---|---|---|
| 0.6.1 | 指令/Fragment契约、输入预算、双Provider映射、Event v10、Context Inspect | 已实现，等待CI |
| 0.6.2 | 项目指令发现、Workspace/Git/环境 Source、freshness、Tool Result裁剪与完整Artifact引用 | 未开始 |
| 0.6.3 | 轮前与reactive Compaction、版本化Summary、关键约束保持Eval | 未开始 |
| 0.6.4 | Thread Resume、Fork、Archive与副作用继承边界 | 未开始 |
| 0.6.5 | Turn Retry、Interrupted Recovery、Provider切换和长会话综合验收 | 未开始 |

开发顺序遵循：源码求证 → 架构决策 → 领域契约 → 最小正式实现 → 失败与恢复测试 → 真实场景验证 → 文档同步。

## 2. 0.6.1 模块边界

~~~text
AgentRuntime PREPARING_CONTEXT
        │
        ├── 已完成语义历史 ──确定性JSON──┐
        ├── 已准入Tool定义 ─确定性JSON───┤
        └── Thread/Turn/Step/Workspace ───┤
                                         ▼
                                  ContextPlanner
                                         │
                 ┌───────────────────────┴──────────────────────┐
                 ▼                                              ▼
          PreparedContext.instructions                 ContextInspection
                 │                                              │
                 ▼                                              ▼
       OpenAI system / Anthropic system           Event v10 → Session Store
~~~

Context Planner 只决定模型输入视图。它不能执行工具、授予权限、读取 Secret 或修改 Session；Event 提交仍由 Agent Runtime 完成。

## 3. 核心契约

### 3.1 ContextFragment

`ContextFragment`包含`kind/source/content`。`kind`固定映射 trust、priority 和 required，正文不能提升自身权限。契约单片正文上限256 KiB，静态Engine把全部来源与正文进一步限制为128 KiB，单次最多128片；无效UTF-8、NUL和空白正文拒绝。

当前静态实现支持六类 Fragment，但0.6.1仅正式完成调用方显式提供的 Runtime/User/Project 指令。Workspace/Git/Environment 的自动收集、时效和来源失败仍属于0.6.2。

### 3.2 ContextBuildInput

字段包括 Thread ID、Turn ID、目标模型步骤、绝对 Workspace，以及历史/Tool Definition的确定性JSON文档。文档总量上限8 MiB，在规划前拒绝异常宿主输入。

### 3.3 PreparedContext

包含瞬时`instructions`和可持久`inspection`。模型指令允许为空；检查记录始终存在。模型指令 SHA-256 必须等于检查记录指纹，空指令与零指令估算必须一致。

### 3.4 ContextInspection

`harnessix.context-inspection/v1`记录 estimator、limits、各输入分项、总量、指纹和 Fragment 决策。模型步骤在同一 Turn 内唯一；记录总量必须不超过可用输入预算。

## 4. 规划算法

1. 按 UTF-8 字节数估算历史和工具定义；
2. 计算可用输入预算；
3. 固定输入已超限则失败；
4. 装入全部 Runtime/User 必选指令，超限则失败；
5. 其余 Fragment 按 priority 降序、source、fragment_id稳定排序；
6. 逐个试装，超限记录`omitted_budget`，不截断正文；
7. 生成结构化JSON指令包、SHA-256和检查记录；
8. Runtime先提交`ContextPrepared`，再进入模型调用状态。

同一输入得到相同排序、指纹和预算结果。0.6.1不做正文内语义冲突解析；冲突的正式含义是外层优先级固定且项目正文不能伪造结构字段。

## 5. Provider 映射

| Provider | 指令位置 | 历史位置 |
|---|---|---|
| OpenAI-compatible Chat Completions | 第一个`system` message | 原规范化messages随后排列 |
| Anthropic Messages | 顶层`system`字段 | 原`messages`保持user起止和Tool配对 |

指令映射参与既有`max_request_bytes`发网前校验。Adapter 不改变 Context 决策。

## 6. 数据与迁移

- Agent Event/Thread当前版本：v10；
- Session Migration：`0012_context_inspection.sql`；
- `Turn.context_inspections`按模型步骤保存；
- v1-v9事件继续按原Schema解析和导出；
- Migration 0012不改写旧事件或快照；下一次事件提交才写v10投影；
- Context正文不在检查记录中重复持久化。

## 7. 失败语义

| 场景 | code | 分类 | 网络请求 |
|---|---|---|---|
| 固定输入或必选指令超预算 | `context_budget_exceeded` | budget | 不发送 |
| 指定Step无检查记录 | `context_not_found` | input | 不涉及 |
| Context事件次序、Step或重复非法 | `invalid_event` | input | 不发送 |
| Context记录提交失败 | 原`storage_*` | storage | 不发送 |
| Planner未知异常 | `runtime_error` | internal | 不发送 |
| 提交后、调用前宿主故障 | Turn失败，检查记录保留 | internal/interrupted | 不自动重发 |

自动压缩尚未实现，因此预算失败不能静默删除历史，也不能借用Turn累计Token预算冒充模型Context Window。

## 8. 取消与关闭

Context Planner是同步纯计算，不持有线程、文件描述符或网络连接。Runtime在规划前和提交前检查CancelToken；取消沿用Turn的持久`CANCELLING → CANCELLED`语义。未来Source I/O和Compaction Provider调用必须增加独立可取消资源生命周期。

## 9. 可观测性

- Span：`harnessix.agent.context`，只带Thread/Turn/模型步骤和结果；
- Token指标：固定`component`枚举；
- Fragment指标：固定`kind/disposition`枚举；
- Context正文、source、路径、指纹和任意用户字符串不进入Metric标签；
- 业务错误原文不作为Span事件或堆栈导出。

## 10. 安全边界

结构化JSON编码能阻止项目正文闭合或篡改外层Fragment字段，但不能保证模型永不服从恶意正文。强制安全边界仍是Tool Registry、Policy、Approval、Workspace和0.7 Sandbox。

检查记录保留source用于解释来源，宿主不得把凭据放入source。0.6.2必须为自动发现文件增加路径作用域、读取缺口、revision和脱敏策略。

## 11. 测试策略

0.6.1自动测试覆盖：

- 固定优先级、稳定排序和JSON结构转义；
- 必选保留、大型可选省略、较小低优先级继续装入；
- 历史及必选指令超限发网前失败；
- 指令正文不进入ContextInspection或Telemetry；
- 两个Provider各自正确映射system指令；
- 每模型步骤唯一Context记录、SQLite重开、Event Replay和诊断读取；
- Event v9拒绝v10 Context事实、旧Schema冻结、Migration 0012；
- Context提交后故障保留检查记录且不调用Provider；
- OTel Span与低基数Metric。

默认CI不需要模型API Key、网络、SSH或外部中间件。

## 12. 0.6.2 入口条件

开始0.6.2前，0.6.1必须满足：完整`make check`、严格异步回归、Schema连续生成一致、独立wheel导入与远端四任务CI通过。0.6.2不得直接用`Path.read_text`绕过0.5 Workspace边界，也不得让项目文件控制Fragment trust或required。
