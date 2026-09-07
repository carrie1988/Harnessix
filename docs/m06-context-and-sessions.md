# 0.6 Context Engine 与持久会话详细实施设计

- 更新日期：2026-09-07
- 状态：0.6.1、0.6.2a已完成；整体0.6进行中
- 目标：支持长任务、多轮会话和可解释、可恢复的上下文管理

## 1. 实施顺序

0.6继续采用可发布纵向切片，每片均包含正式契约、失败语义、持久化、可观测性、测试和文档。

| 切片 | 内容 | 状态 |
|---|---|---|
| 0.6.1 | 指令/Fragment契约、输入预算、双Provider映射、Event v10、Context Inspect | 已完成 |
| 0.6.2a | 异步Source端口、受控项目指令发现、freshness、Context Inspection v2、Event/Thread v11 | 已完成 |
| 0.6.2b | Workspace/Git/环境Source与跨来源一致性 | 未开始 |
| 0.6.2c | Tool Result模型视图裁剪、稳定决策与完整Artifact引用 | 未开始 |
| 0.6.3 | 轮前与reactive Compaction、版本化Summary、关键约束保持Eval | 未开始 |
| 0.6.4 | Thread Resume、Fork、Archive与副作用继承边界 | 未开始 |
| 0.6.5 | Turn Retry、Interrupted Recovery、Provider切换和长会话综合验收 | 未开始 |

开发顺序遵循：源码研究 → 架构决策 → 领域契约 → 最小正式实现 → 失败与恢复测试 → 真实场景验证 → 文档同步。

## 2. 0.6.1 静态规划模块边界

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

## 6. 0.6.1数据与迁移

- 0.6.1 Agent Event/Thread版本：v10；当前动态Source实现已推进至v11，见第13节；
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

## 8. 0.6.1取消与关闭

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

## 12. 0.6.2入口条件

0.6.1实现提交`16c5838`已完成完整`make check`、严格异步回归、Schema连续生成、独立wheel导入及远端[CI 34090360609](https://github.com/carrie1988/Harnessix/actions/runs/34090360609)四任务验收。0.6.2不得直接用`Path.read_text`绕过0.5 Workspace边界，也不得让项目文件控制Fragment trust或required。

## 13. 0.6.2a总体方案

~~~text
AgentRuntime PREPARING_CONTEXT
        │
        ├── ContextBuildInput
        └── CancelToken
                │
                ▼
       SourcedContextEngine
                │
                ├── ProjectInstructionSource（异步I/O边界）
                │       └── Workspace + read_file分页 + revision
                │
                ├── ContextFragment（瞬时正文）
                └── ContextSourceSnapshot（持久、无正文）
                            │
                            ▼
                     ContextEngine
                        │       │
                        │       └── ContextInspection v2
                        ▼                    │
                 Provider instructions      └── Event v11 → Session
~~~

`ContextSource`负责观察外部状态，不决定预算、优先级或信任等级；`ContextEngine`继续负责确定性排序和预算。同步`context`和异步`async_context`是互斥宿主入口，避免通过运行时反射猜测Planner类型。

## 14. 动态Source契约

### 14.1 ContextSourceObservation

一次成功观测包含`workspace_scope`、`source_revision`和最多64份`ContextSourceDocument`。文档包含相对source、瞬时正文和文件revision；总正文最多128 KiB。它不携带kind，防止来源正文或返回值自报高信任等级。

### 14.2 SourcedContextEngine

最多注册16个稳定命名空间source ID，拒绝重复身份。动态Source只允许Project/Workspace/Git/Environment四类。引擎按注册顺序逐个刷新，在每次I/O前后检查取消，随后把非空白文档转为固定kind Fragment并构造快照。组合Fragment仍受128片和128 KiB的原Engine边界约束。

### 14.3 ContextSourceSnapshot

`harnessix.context-source-snapshot/v1`只保存：source ID、kind、`available|empty`、workspace scope、source revision及各文档的source/revision/字节数/可选fragment ID。它不保存正文。`ContextInspection v2`要求source ID唯一、Source Fragment归属唯一且所有引用均能在同一Fragment决策中找到。

## 15. 项目指令详细发现算法

`ProjectInstructionSource`由宿主绑定一个已存在的规范Workspace根、相对工作目录、额外拒绝路径和总字节上限。默认总量64 KiB。

1. Thread Workspace必须严格解析到与绑定根相同的规范目录；
2. 通过Workspace路径解析验证工作目录，构造根到工作目录的祖先链；
3. 记录每个祖先目录的稳定revision；
4. 每层依次尝试`AGENTS.override.md`和`AGENTS.md`，选择首个存在文件；
5. 复用`read_file`按最多2000行/24 KiB页读取，后续页携带首个revision；
6. 任一长行、扫描边界或总量越界均拒绝，不发送部分指令；
7. 再次读取祖先目录revision；候选增删、替换或目录变化使整次观测失败；
8. source revision绑定读取契约版本、工作目录、候选顺序、目录revision和文件revision。

不存在任何候选时成功返回`empty`。已选文件仅为空白时仍记录文档revision和字节数，但不生成Fragment；override存在即阻止同目录普通文件回退。当前不自动寻找`.git`根，因为Context根必须与宿主给Tool Runtime的能力根一致。

## 16. 失败、取消与恢复语义

| 场景 | code | retryable | 行为 |
|---|---|---:|---|
| Workspace绑定不一致 | `context_source_workspace_mismatch` | 否 | 发网前失败 |
| 链接、硬链接、拒绝路径、错类型、非法文本 | `context_source_invalid` | 否 | 发网前失败 |
| 文件、Source或组合结果超限 | `context_source_too_large` | 否 | 发网前失败，不截断 |
| 读取超时、revision漂移、工作区变化或暂时I/O失败 | `context_source_unavailable` | 是 | 发网前失败 |
| 未发现/空白 | 无错误，快照`empty` | 不适用 | 继续规划 |
| Turn取消 | `cancelled` | 不适用 | 停止读取、join线程，不调用Provider |

来源错误全部归为`FailureCategory.INPUT`。当前Session不持久化Source正文，因此重开时只能重新观察；无法观察时不得拿旧revision猜测正文。已成功提交的Context v2检查记录可Replay和诊断，但不授权任何文件或工具能力。

文件读取通过共享`run_read_operation`执行。协程取消时先设置`ReadOperation.stopped`，等待线程释放Workspace FD后再传播取消；CodingToolRuntime同步复用了该辅助逻辑，避免两套清理语义漂移。

## 17. 当前数据版本与兼容

- Agent Event/Thread：v11；
- Context Inspection：v1继续读取，动态Source使用v2；
- Context Source Snapshot：v1；
- Session Migration：`0013_context_sources.sql`，只增加最低reader标记；
- v1-v10事件和投影继续读取，历史Schema文件冻结；
- v2检查记录只能写入Event v11；静态v1检查事实最低仍为v10，但当前程序新写统一使用v11；
- Migration 13不改写旧Event、投影或Artifact。

## 18. 可观测性与安全

新增`harnessix.agent.context.sources`计数器，仅允许固定`kind/status`标签。Source ID、相对路径、正文、fragment ID、文件revision和workspace scope均不得进入Metric标签；Context Span仍只含Thread/Turn/模型步骤和结果。

项目指令是发送给Provider的项目级不可信输入，不是Policy或授权。JSON结构和固定kind可阻止其修改外层trust字段；实际文件、进程、网络和写入仍由Tool/Policy/Approval/Workspace/Sandbox控制。no-follow、单硬链接和deny-path策略比参考实现更严格，不能为兼容说明文件而降级。

## 19. 0.6.2a测试矩阵

定向自动测试覆盖：

- 根到工作目录顺序、同目录override优先和正文结构化转义；
- 无文件、空白文件、文件更新和每模型步骤刷新；
- 符号链接、硬链接、工作区错配、总量越界和读取超时；
- 取消通知并join真实线程，不遗留后台读取；
- v2快照不含正文，source/document revision随文件变化；
- Provider调用前失败、错误分类和retryable；
- Event v11、Context v2最低版本、Migration 13、历史Schema冻结、SQLite Replay与升级硬退出；
- Source指标只有kind/status且不泄漏路径、正文、scope或revision。

默认测试不需要模型API Key、网络、SSH、远程服务器或新中间件。全量回归、严格异步、Schema、wheel及远端四矩阵CI关闭数据见[测试与Eval规范第55节](testing-and-evals.md#55-062a-受控项目指令source与freshness验收2026-09-07)。

## 20. 后续切片

### 20.1 0.6.2b

实现Workspace、Git和环境Source，并明确同一次规划的跨来源一致性、Git非仓库语义、状态输出边界、环境白名单和刷新策略。不得读取任意环境变量或Secret。

### 20.2 0.6.2c

在不改写Session原始Item的前提下构造有界Tool Result模型视图；超大正文必须先成功发布完整Artifact，再形成稳定预览和引用。需要单独处理结构化JSON、文本、媒体、Patch、Process和Artifact过期/失败，不能直接对任意JSON字符串切片。

### 20.3 0.6.3

在来源和工具模型视图稳定后实现Compaction。Summary必须版本化、持久化、可恢复，并通过关键约束保持Eval；压缩不能删除原始Event事实。
