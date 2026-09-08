# 0.6 Context Engine 与持久会话详细实施设计

- 更新日期：2026-09-08
- 状态：0.6.1至0.6.4已完成并通过远端CI；0.6.5实现及本地完整验收完成、远端CI待确认；整体0.6处于发布门禁
- 目标：支持长任务、多轮会话和可解释、可恢复的上下文管理

## 1. 实施顺序

0.6继续采用可发布纵向切片，每片均包含正式契约、失败语义、持久化、可观测性、测试和文档。

| 切片 | 内容 | 状态 |
|---|---|---|
| 0.6.1 | 指令/Fragment契约、输入预算、双Provider映射、Event v10、Context Inspect | 已完成 |
| 0.6.2a | 异步Source端口、受控项目指令发现、freshness、Context Inspection v2、Event/Thread v11 | 已完成 |
| 0.6.2b | Workspace/Git/环境Source与跨来源一致性 | 已完成 |
| 0.6.2c | Tool Result模型视图裁剪、稳定决策与完整Artifact引用 | 已完成 |
| 0.6.3 | 轮前与reactive Compaction、版本化Summary、关键约束保持Eval | 已完成；[CI 34183895692](https://github.com/carrie1988/Harnessix/actions/runs/34183895692)通过 |
| 0.6.4 | Thread Resume、Fork、Archive与副作用继承边界 | 已完成；[CI 34188329001](https://github.com/carrie1988/Harnessix/actions/runs/34188329001)通过 |
| 0.6.5 | Turn Retry、Interrupted Recovery、Provider切换和长会话综合验收 | 实现及本地完整验收完成；远端CI待确认 |

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

静态实现支持六类Fragment，但0.6.1仅正式完成调用方显式提供的Runtime/User/Project指令。Workspace/Git/Environment自动收集、时效和来源失败已由0.6.2a/0.6.2b完成。

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

- 0.6.1 Agent Event/Thread版本：v10；多Source门禁推进至v12，仓库当前版本为v17；
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
- v2检查记录只能写入Event v11及以上；静态v1检查事实最低仍为v10。0.6.2a新写使用v11，0.6.2b使用v12，当前程序统一写v17；
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

## 20. 0.6.2b总体方案

~~~text
AgentRuntime PREPARING_CONTEXT
        │
        ▼
SourcedContextEngine
        │
        ├── 第一轮：Project / Workspace / Git / Environment
        ├── 第二轮：Project / Workspace / Git / Environment
        ├── scope、source revision、完整观测逐项核对
        │
        ├── ContextFragment（瞬时正文）
        └── ContextSourceSnapshot + ContextConsistencySnapshot（无正文）
                                      │
                                      ▼
                              ContextInspection v3
                                      │
                         Event v12 → Session migration 14
                                      │
                                      ▼
                                   Provider
~~~

一个Source保持0.6.2a单次观测和Context Inspection v2，避免改变已有宿主的读取次数与序列化。两个及以上Source才启用双观测并生成v3；多来源结果不满足一致性契约时不得降级为v2继续发网。

## 21. WorkspaceContextSource详细设计

Source由宿主绑定规范根、相对工作目录、deny-path、每目录条目上限和正文上限。默认只列Workspace根和当前工作目录的一级条目，二者相同时去重；不递归读取代码内容。

每个目录先调用一次`list_files`，随后携带首次revision复核相同页。底层Workspace继续执行路径分段、拒绝名/后缀、no-follow、单链接、根身份与目录链TOCTOU核对。模型文档`workspace/layout.json`包含固定schema、相对工作目录、目录路径、名称/类型和`truncated`；按名称稳定排序。

默认每目录64项、正文12 KiB。目录本身超过页边界或正文放不下时，从稳定排序末尾省略模型展示项并设置`truncated=true`；底层扫描超过10000项、2 MiB名称或五秒截止时间时整次失败，不把未完成扫描视为正常截断。source revision绑定契约、工作目录、条目/正文上限、目录revision和文档revision。

## 22. GitContextSource详细设计

Git Source绑定相同Workspace根、受信绝对Git可执行文件和既有`GitReadRuntime`。只调用固定`git status --porcelain=v2 --branch --untracked-files=all -z`路径；模型、仓库文件和环境不能提供命令、参数、cwd、配置或可执行文件。

`git/status.json`包含`repository`、branch、HEAD、upstream、ahead/behind、状态条目、`total_entries`和`truncated`。默认请求100项、正文16 KiB。正文越界时从返回条目末尾省略并显式截断；底层完整status摘要仍参与source revision。非Git目录返回`repository=false`文档；初始仓库HEAD为`null`，detached HEAD的branch为`null`。

Git运行时使用固定最小环境、空全局配置、关闭系统配置/Hook/fsmonitor/外部diff、禁止交互/分页/可选锁、五秒超时和有界捕获。Workspace绑定在Git调用前后核对。状态项还通过Workspace路径策略过滤：`.env`、`.git`、密钥后缀或宿主deny-path不会借Git状态绕过文件Source边界；被过滤数量只反映为`total_entries`与`truncated`，路径正文不进入模型。

远端URL、Git用户、提交日志、diff和任意Git配置不采集。Git Context是External trust提示，不是仓库可信证明或写权限。

## 23. EnvironmentContextSource详细设计

环境Source接收宿主提供的值映射和显式allowlist，只按排序后的allowlist逐项取值，不遍历映射，不自动读取`os.environ`。allowlist最多32个唯一大写环境键；包含token、secret、password、passwd、key、credential、cookie、auth、authorization、private、jwt或session语义的名称在构造阶段拒绝。

值必须为UTF-8字符串，不含NUL及控制字符，单值最多1024字节。`environment/runtime.json`包含固定schema、`os.name`、`sys.platform`、相对工作目录和当次存在的准入值，正文最多4 KiB；缺失allowlist键被省略，总量越界失败而非静默删除。映射值在模型步骤之间变化会产生新文档/source revision。

名称过滤不能判断值本身是否敏感。生产宿主只能准入低敏环境事实，例如CI模式或构建标签；不能把凭据换成无害名称后传入。

## 24. 跨Source一致性契约

`optimistic-double-observation/v1`按稳定注册顺序执行两轮完整观测。每轮全部Source必须返回同一个`workspace_scope`；同一Source两轮revision必须一致；revision一致时完整Observation也必须相等。验证成功后只使用第二轮正文与快照规划。

该策略证明两轮有界窗口内没有检测到状态变化，不提供文件系统、Git和环境的事务原子性。第二轮完成后到Provider请求之间仍可能有外部变化。模型后续操作必须重新通过工具自己的revision、Policy、Approval和效果核对，不能凭Context快照执行写入。

`ContextConsistencySnapshot v1`只记录固定strategy、`passes=2`、来源数量和共同scope，不保存正文或时间戳。`ContextInspection v3`要求至少两个唯一Source、全部scope等于一致性scope、Source Fragment归属唯一且文档字节数/路径/kind与Fragment决策一致。

## 25. 失败、取消与恢复

| 场景 | code | retryable | Provider请求 |
|---|---|---:|---:|
| Source绑定根或scope不一致 | `context_source_workspace_mismatch` | 否 | 不发送 |
| 两轮source revision变化 | `context_sources_changed` | 是 | 不发送 |
| 同revision正文变化、非法返回、链接/路径/环境值 | `context_source_invalid` | 否 | 不发送 |
| 目录、Git捕获、环境值或组合正文超限 | `context_source_too_large` | 否 | 不发送 |
| Workspace/Git超时、绑定漂移或暂时I/O失败 | `context_source_unavailable` | 是 | 不发送 |
| 非Git目录 | 无错误，`repository=false` | 不适用 | 继续规划 |
| Turn取消 | 既有`cancelled` | 不适用 | 不发送下一请求 |

任一第一轮或第二轮失败都会停止整个模型步骤，不提交部分Context事实。Session没有Source正文，重启后重新观测；旧revision不能作为正文回退。已提交的v3检查记录可Replay和诊断，但不授权工具。

文件与环境读取沿用`run_read_operation`取消后通知并join线程。Git沿用`HostProcessRuntime`进程组终止、等待和关闭语义；Source不吞掉`TurnCancelled`。

## 26. 数据、兼容与可观测性

- Agent Event/Thread：v12；
- Context Inspection：v1、v2继续读取，多Source写v3；
- Context Source Snapshot：v1不变；
- Context Consistency Snapshot：v1；
- Session Migration：`0014_context_source_consistency.sql`，只推进最低reader标记；
- v1-v11 Event/Thread及Context Inspection v1/v2 Schema冻结；
- migration 14不改写Event、投影、Artifact或Effect Journal。应用后旧reader必须拒绝，回退只能恢复一致备份。

`harnessix.agent.context.sources`继续只带固定kind/status。新增`harnessix.agent.context.consistency`只带固定strategy/result。Source ID、路径、变量名、正文、revision、scope和任意Git stderr不得进入Metric标签或Span事件。

## 27. 0.6.2b测试矩阵

自动测试覆盖Workspace排序/过滤/截断/revision竞态，Git普通/脏/非仓库/初始/detached/状态截断/超时，环境allowlist/Secret名称/非法值/总量和每步骤刷新，多Source scope错配/revision漂移/同revision正文漂移，以及v3持久化、Event v12、migration 14、Replay、Schema冻结和Telemetry脱敏。

真实场景使用临时Git仓库执行实际`git init/add/commit/checkout/status`，并通过真实v11 wheel创建Context Inspection v2会话，再由v12 wheel原字节升级、追加v3事件和验证旧reader拒绝。默认验收不需要模型API、SSH、远程服务器或新中间件。关闭数据见[测试与Eval规范第56节](testing-and-evals.md#56-062b-workspacegit环境source与跨来源一致性验收2026-09-07)。

## 28. 工具视图与后续切片

### 28.1 0.6.2c

采用Session事实历史与瞬时模型历史双层结构。Tool Result首次进入模型历史时，按Provider可见规范JSON的UTF-8字节数冻结`inline`或`artifact_reference`决定；后续步骤和恢复复用精确决定。每步先验证所有可见Artifact并提交`ModelHistoryPrepared`，再让Context与Provider共同使用准备后历史。

超限结果不切割任意JSON。只有完整、已发布、未过期且与Thread/Call/`tool_result`用途双向绑定的Artifact，并能证明被省略字段被正文完整覆盖时，才能省略对应内容并保留引用。Grep/Glob只省略归档记录列表，查询和统计字段原样保留；通用preview只有整体等于单条正文或记录前缀时才置空。Process和Batch Diff Artifact仅验证各自证据，不为任意结果字段兜底；当前JSON结果不声明媒体支持。正式契约、失败语义和迁移见[ADR 0057](adr/0057-tool-result-model-view-and-artifact-binding.md)。

### 28.2 0.6.3

在来源和工具模型视图稳定后实现Compaction。Summary版本化、持久化、可恢复，并通过关键约束保持Eval；压缩不删除原始Event事实。源码证据见[Compaction研究](research/compaction-and-context-windows.md)，决策见[ADR 0058](adr/0058-compaction-windows-and-accounted-summary-attempts.md)，模块、接口、流程、失败、安全、部署与测试见[自动Compaction运行时详细设计](compaction-runtime-and-windows.md)。

## 29. 0.6.2c实现边界

| 模块 | 实现职责 | 持久化边界 |
|---|---|---|
| `context/tool_result_contracts.py` | v1策略、冻结决定、每步检查与字段一致性 | 通过Event v13写入Turn投影 |
| `context/tool_result_view.py` | 有界纯投影、规范JSON计量、完整性分类、旧决定应用 | 不执行I/O、不修改原Item |
| `ArtifactAccessScope` | 验证宿主绑定的当前工作区能力 | 不从历史反推访问授权 |
| `ArtifactReferenceVerifier` | 同Session事务快照内验证归属、manifest、TTL、正文与省略覆盖 | 只读，不补写旧结果Artifact |
| `AgentRuntime` | History → Artifact验证 → History事件 → Context → Provider | 每步新决定与检查记录原子追加 |
| Reducer | 状态/步骤、来源、唯一性、替换范围和全部摘要验证 | 在线提交和离线Replay复用 |

`AgentRuntime`新增`tool_result_view_policy`、`artifact_verifier`、`artifact_access`三个可选配置。默认每结果64 KiB；配置范围1 KiB至1 MiB。原始历史准备总量最多8192项/8 MiB；Artifact I/O整组最多5秒。Context和Provider引用同一份深拷贝历史，两个Provider按原有消息格式映射，不修改其线上JSON键顺序；检查摘要针对排序规范JSON而非供应商HTTP请求原字节。

SQLite Artifact发布器可自动作为验证器。Coding Tool Runtime提供实际Workspace scope；Batch Diff发布器委托原Managed Patch Bridge提供副本scope。独立宿主可显式注入这两个只读端口，但必须维持相同Session和真实工作区访问边界。

0.6.2c基线为Agent Event/Thread v13（仓库当前已推进为v17）；Context Inspection仍兼容v1/v2/v3。新增Session `0015_tool_result_model_view.sql`只推进最低reader，不重写旧Event、投影或Artifact；v1-v12 Schema保持冻结。旧历史缺少冻结决定时只允许inline，不因新默认预算而追溯裁剪。

已提交决定通过`Turn.tool_result_view_decisions`读取；每步统计通过`Turn.model_history_inspections`读取。决定可能包含Artifact manifest及残留查询元数据，应按Session本身权限保护。Metrics仅输出固定strategy/component和数量；不输出正文、路径、ID或摘要标签。完整失败代码与退出窗口见ADR 0057。

0.6.2c自身不包含自动补归档、任意文件结果截断、图片/音频或总历史Token预算压缩。后续0.6.3已增加Compaction，但不会改变单个Tool Result的完整归档要求；超限且无完整归档的结果仍明确失败。

0.6.2c实现提交`5e283ff`通过[CI 34173011955](https://github.com/carrie1988/Harnessix/actions/runs/34173011955)的Python 3.12、Python 3.13、macOS Coding Tools和PostgreSQL四项任务。结合测试规范第57节的完整本地、发布物与恢复门禁，本片关闭；整体0.6与V1.0商用目标仍未完成。

## 30. 0.6.3首窗口规划内部门禁

已实现`CompactionPolicy/Anchor/Plan/Summary v1`、闭合组规划和候选校验。正式接口、选择算法、预算单位、取消语义、来源指纹及安全边界见[压缩窗口规划详细设计](compaction-window-planning.md)。

当前流程不调用Provider、不写Session、不发布活动窗口。原模型视图决定、Artifact验证义务和原始事件保持不变。候选结构为首条原用户消息、低信任助手摘要和原顺序保留的固定组/后缀；不会为满足Provider格式伪造用户指令。首条/当前用户原文自动固定，显式锚点扩展到完整调用组。

本门禁完成后依次推进独立摘要Attempt包装、Token增量记账、成本报告、候选与尝试绑定、CAS窗口发布和中断恢复。仅计划JSON往返与只读Session重开不等于付费摘要恢复验收；这些缺口已由第31、32节的后续门禁补齐。

后续摘要账本已实现，正式事件、Cost v2、Campaign影响面、事务和恢复矩阵见[摘要尝试账本详细设计](compaction-attempt-ledger.md)。Provider Event v3保持不变；该账本门禁使用Event/Thread v14与migration16，活动窗口另使用v15与migration17。

首窗口实现提交`13e50eb`的[CI 34175148706](https://github.com/carrie1988/Harnessix/actions/runs/34175148706)四项任务通过；严格本地回归2783 passed、2项仅因PostgreSQL未配置跳过，详见测试规范第58节。


## 31. 0.6.3独立摘要账本内部门禁

正式实现见[ADR 0059](adr/0059-compaction-attempt-ledger-and-purpose-costs.md)及[详细设计](compaction-attempt-ledger.md)。独立集合不增加普通模型步骤；开放账本隔离来源变化；普通与摘要复用累计差额、Billing和响应身份验证。成功请求与候选接受分离，失败摘要仍进入Cost v2和Campaign支出。

Event/Thread v14、Cost Report v2、Session migration16已加入；旧v1-v13及Cost v1 Schema不变。真实SQLite子进程退出覆盖计划、意图、用量、结算、候选与恢复事务；多次重开不自动请求。没有摘要记录时仍返回Cost v1，原Smoke不启用压缩。

该门禁只接受账本，不单独关闭0.6.3。摘要HTTP、请求预算、活动窗口、重复压缩及轮前/reactive触发已由后续v15切片实现，见下一节。0.6.4和0.6.5已按原纵向切片顺序完成实现及本地验收。

## 32. 0.6.3自动Compaction与活动窗口

正式实现见[ADR 0058](adr/0058-compaction-windows-and-accounted-summary-attempts.md)和[详细设计](compaction-runtime-and-windows.md)。`CompactionRuntimeConfig v1`显式绑定规划策略、轮前阈值和摘要输出上限；配置与摘要Provider必须同时存在，默认不启用付费请求。

Runtime在摘要HTTP前验证整个来源历史的Artifact，提交计划和Provider请求意图后才继续消费流。摘要没有工具，只允许单Attempt、单文本和完整一致用量。候选经纯计算重算后持久化，活动窗口以紧邻事件和Session CAS发布。窗口仅保存Item身份、高水位和摘要，不复制或删除原历史；重复压缩使用上一活动窗口与新增原始事实。

reactive路径只接受未产生语义Item且已完整结算的`provider_context_overflow`。失败请求仍消耗普通步骤和Token；没有新压缩进展时停止，不再次付费摘要。Model History Inspection v2绑定活动窗口并保留原始历史数量证据。

0.6.3交付使用Agent Event/Thread v15和Session migration17。真实`b20948e` v14 wheel创建已结算候选后，v15升级保持旧事件和投影原字节，首次重开零Provider请求发布唯一窗口，v14 reader明确拒绝migration17。运行时及窗口事务九个进程退出切点、双Adapter、取消、超时、Artifact损坏、重复窗口和六类工程语义Oracle均已验证。370项专项测试及严格全量2901 passed、2 skipped通过；[CI 34183895692](https://github.com/carrie1988/Harnessix/actions/runs/34183895692)四项任务全部通过。仓库当前版本已由0.6.5推进为Event/Thread v17和migration19。

## 33. 0.6.4 Thread生命周期与无授权Fork

源码依据、决策和实现分别见[Thread生命周期源码研究](research/thread-lifecycle-and-fork.md)、[ADR 0060](adr/0060-thread-lifecycle-and-authority-free-forks.md)和[详细设计](thread-lifecycle.md)。Resume只重新附着到同一持久Thread；Fork在终结Turn边界冻结有界模型历史；Archive将无活跃Turn的Thread原子转换为只读状态。三类操作均不会自动调用Provider或重放已完成工具。

Fork历史以`authority=none`写入子Thread首事件，不进入子Thread的Turn集合。Tool Call/Result必须完整配对，Tool Result模型视图决定与Artifact真实所有者同时冻结。Artifact正文不复制；后续模型请求仍按原所有者、当前Workspace scope、TTL、正文摘要和覆盖证明执行发网前校验。来源Thread的sequence、规范投影摘要、边界和完整快照由SessionStore在同一SQLite事务中重算，确定性子Thread和事件身份支持提交后安全重试。

Agent Event/Thread推进至v16，Session migration18只提高最低reader，不改写旧事件、投影或Artifact。七个真实进程退出切点覆盖Fork来源检查、目标Event、Projection、Commit和Archive Event、Projection、Commit；退出后只存在完整回滚或完整提交。v15→v16独立wheel验证保持旧事件与投影原字节，Resume零模型请求，Fork和Archive可Replay/Rebuild，旧v15 reader拒绝migration18且不修改数据库。

本地严格全量验收为2914 passed、2 skipped，Ruff和Mypy通过，Schema连续生成两次聚合SHA256均为`e9e54ad0afd92c0d9de41477d32e2c52a43cdc0ec0b2e38bd2764c7a59d0004a`。两个skip仅因本地未配置`HARNESSIX_TEST_POSTGRES_URL`。实现提交`24e0899`的[CI 34188329001](https://github.com/carrie1988/Harnessix/actions/runs/34188329001)四项任务全部通过。

## 34. 0.6.5终态Turn Retry、Provider切换与综合恢复

源码依据、架构决策和正式接口分别见[专项研究](research/turn-retry-and-provider-switch.md)、[ADR 0061](adr/0061-terminal-turn-retry-and-provider-neutral-history.md)和[详细设计](turn-retry-and-provider-switch.md)。`retry_turn`只从当前Thread最新的`failed`、`cancelled`或`interrupted` Turn创建新Turn；来源终态不重开，新Turn通过`retry_of_turn_id`形成持久直接来源边，并写入固定续作User Item。普通Turn请求指纹算法保持不变，Retry指纹额外绑定来源；幂等查询先于最新来源校验，覆盖Commit后响应丢失。

来源存在任意`ToolResult.outcome=unknown`时以`retry_unsafe_effect`失败关闭。已知完成效果继续作为规范历史，但不会进入新Turn的待执行调用；后续写操作仍需通过原revision、Policy、Approval和Action边界。`resume_turn`仍只处理`waiting_approval`和`waiting_action`，其他活动状态在宿主重开时收敛为`interrupted`，不恢复旧Provider流。

模型历史继续由规范Item构建。历史Tool Call使用`call_<UUID hex>`稳定内部身份，OpenAI-compatible与Anthropic Adapter分别映射目标协议；原生`wire-call-*`、`toolu_*`、Thinking签名、流游标和请求metadata不会跨Provider发送。双向真实SDK MockTransport测试验证历史Call/Result配对、原生ID不泄漏、实际Provider尝试可审计且来源工具只执行一次。

Agent Event/Thread推进至v17，Session migration19只追加最低reader标记，不改写旧Event、Projection、Artifact、Compaction窗口或模型尝试。三个Retry接受事务硬退出切点验证Event/Projection前完整回滚、Commit后完整可见，重开只将已接受Turn收敛为Interrupted且不调用Provider。v16→v17独立wheel验证旧wheel SHA256为`e43338aa23c0da5d03a7fcfef1cc32c6fcff0e7c2da18f713cdc8f9ddba4fd2c`、v17 wheel SHA256为`40c7b59fed4c81746b643a2639aa292a1039c28f4eb1fc3a5a6fbfa928ea7338`；旧字节保持不变，v16 reader拒绝migration19且不修改数据库。

长会话综合测试串联有账本Compaction、活动窗口、Tool Result、接受后进程中断、启动恢复、跨Provider Retry、Fork和Archive，验证模型历史保持有界、原事件前缀原字节保留、工具不重执行、Usage按全部普通与摘要尝试一致累计、Cost报告可重算以及父子Thread均可Replay/Rebuild。本地严格全量为2924 passed、2 skipped，270.46秒；Ruff和Mypy 164个源文件通过；Schema连续生成两次聚合SHA256均为`68f1eed44d4e8dee742db5adfd01f85f4f6844d2509b18c6ccce6b4744151f0c`；migration19 SHA256为`926e3bbb1ee98971815166b9737032b8bc63ace9d6fb84bc887380606d654c7a`。两个skip仅因本地未配置`HARNESSIX_TEST_POSTGRES_URL`，远端四矩阵待本次实现提交确认。
