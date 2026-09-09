# Harnessix Code 0.8 产品运行时与扩展详细设计

## 1. 文档状态

- 适用版本：0.8；
- 当前状态：0.8.1～0.8.6已完成实现和本地完整验收，远端六矩阵门禁待关闭；
- 总体目标：把0.7已经完成的可信执行与工程交付能力开放为可恢复的本地产品服务，并在相同Runtime、Permission和Sandbox边界内接入CLI、SDK、MCP、Skills、Hooks与Provider配置。

本文只把已经实现并验证的切片标记为完成。每个切片必须依次完成源码研究、架构决策、领域契约、最小正式实现、失败恢复测试、真实场景验证和文档同步。

## 2. 范围与非目标

### 2.1 0.8范围

1. Agent Protocol v1公共合同、兼容与事件恢复；
2. stdio Headless App Server和Python Agent SDK；
3. 不复制Runtime状态机的薄CLI与双向交互；
4. MCP Client和可选MCP Server；
5. Skills、Hooks及供应链边界；
6. Provider、Profile、Secret引用和配置迁移。

### 2.2 非目标

- 完整TUI视觉、安装器、签名发行物和自动更新属于0.9；
- WebSocket、HTTP Agent Server、IDE、Web和集中式多租户控制面不属于0.8；
- MCP、Skill或Hook不得获得0.7可信Action入口之外的文件、进程、网络或Secret权限；
- 0.8不以增加模型API调用次数替代确定性协议和恢复测试。

## 3. 总体结构

```text
Thin CLI / Python Agent SDK / Automation
                  │
      Agent Protocol v1 (JSON-RPC 2.0)
                  │
       stdio Headless App Server
                  │
   Agent Application Service / AgentRuntime
        │                 │
 Session Store       Extension Runtime
                          │
          MCP / Skills / Hooks / Provider Config
                          │
                 TrustedActionRouter
                          │
      Workspace / Process / Sandbox / Delivery
```

App Server只做协议验证、身份绑定、幂等、投影、订阅和Runtime编排，不重新实现Agent Loop。CLI和SDK只消费公共协议，不直接打开Session SQLite、Effect Journal或Secret Store。

## 4. 0.8.1 Agent Protocol v1详细设计

### 4.1 生命周期

连接状态固定为`new → initialized_pending_ack → ready → closing → closed`。只有`initialize`可从`new`进入协商态；只有`notifications/initialized`可进入`ready`。EOF、输出失败或显式关闭进入`closing`，拒绝新请求，等待已接受命令完成到持久边界后关闭。

### 4.2 Envelope与限制

- 标准JSON-RPC 2.0；不接受Batch；
- Request ID为1～128字符字符串或绝对值不超过`2^53-1`的整数；
- 方法名1～128字符，参数只接受对象；
- 默认单帧1 MiB、字符串1 MiB、数组8192项、对象递归64层；
- stdout仅承载JSONL，stderr仅承载脱敏诊断；
- 解析错误`id=null`，已识别Request的错误回传原ID，Notification不返回Response。

### 4.3 初始化协商

`initialize`输入包含精确协议版本、客户端名称/版本、稳定`clientInstanceId`和能力。服务端返回自身信息、已实现方法、通知、双向请求、Artifact、Replay和双方有效限制。v1只接受精确`1.0`；未来次版本通过显式兼容集合协商，不用字符串大小比较猜测兼容。

### 4.4 公共投影

- `ThreadView`：Thread身份、Workspace、当前游标、活动Turn、归档、时间和Turn投影；
- `TurnView`：请求身份、状态、预算、用量、Item、错误和时间；
- `ItemView`：公共内容联合、状态和白名单错误；
- `PublicEvent`：Thread/Turn/Item/Usage公开变化，绑定event ID、Thread、可选Turn和单调cursor。

内部模型尝试、Context检查、Compaction账本等事件不逐条公开。Replay仍扫描全部内部事件并返回`scannedThrough`，因此公开事件游标允许跳跃且不会导致重复扫描。

### 4.5 Command与Query

Command必须携带`requestId`，包括Thread创建/分叉/归档、Turn开始/重试/取消、审批答复以及后续Steering和提问答复。Query不创建领域事实，包括Thread读取/列表、事件Replay和Artifact分页。`thread/resume`只重新附着并触发已定义恢复，不创建新Turn。

JSON-RPC ID与`requestId`独立。SDK在响应超时后重用原`requestId`并分配新的JSON-RPC ID；服务端返回原领域结果或冲突，不重复副作用。

协议命令账本以`clientInstanceId + requestId`为主键，只保存方法名、规范参数SHA-256和有界公开终态，不保存Prompt或原始参数。Session migration20追加`protocol_requests`表；`accepted`记录在调用Runtime之前提交，`completed/failed`终态具备结果摘要校验。相同键和相同参数返回既有记录，相同键绑定不同方法或参数以`idempotency_conflict`失败。`accepted`状态不等于副作用未发生，后续App Server必须使用领域确定性身份或查询恢复，禁止直接重放。

### 4.6 错误

| JSON-RPC码 | 稳定data.code | 含义 |
|---|---|---|
| `-32700` | `parse_error` | JSON或UTF-8不可解析 |
| `-32600` | `invalid_request` | Envelope或消息形态非法 |
| `-32601` | `method_not_found` | 方法未实现或未协商 |
| `-32602` | `invalid_params` | 参数Schema失败 |
| `-32603` | `internal_error` | 未分类服务端错误，正文脱敏 |
| `-32010` | 领域错误码 | Runtime确定性拒绝或失败 |
| `-32011` | `idempotency_conflict` | 幂等键已绑定其他命令 |
| `-32012` | `not_initialized` | 未完成握手 |
| `-32013` | `server_overloaded` | 入站或执行容量已满，可重试 |
| `-32014` | `client_too_slow` | 关键出站消息无法在界限内排队 |
| `-32015` | `server_closing` | 已开始关闭，不再接受命令 |

错误data不保存Prompt、参数正文、Provider响应、路径正文或Secret；字段路径只使用Schema字段名和数组下标。

### 4.7 0.8.1验收

- 公共Pydantic合同和提交仓库的JSON Schema逐项相等；
- Envelope、ID、方法、参数、未知字段、版本和错误Golden测试；
- Thread/Turn/Item/Event投影与内部Event v17解耦；
- 隐藏事件导致游标跳跃时，Replay返回正确`scannedThrough`；
- 旧客户端解析未知通知与新增可选结果字段的兼容测试；
- 默认测试不启动模型、不访问网络、不读取用户凭据。

0.8.1实现位于`harnessix.protocol`，包括严格公共合同、JSON帧Codec、兼容解码、内部事件白名单投影、Replay投影和SQLite协议请求账本。提交仓库的13份Schema由同一运行时模型生成。确定性测试覆盖非法UTF-8/JSON、Batch、重复字段、深度和尺寸、ID边界、未知输入字段、旧客户端新增输出字段、未知通知、隐藏内部事件造成的游标跳跃、重复Command、持久重开、终态冲突和账本篡改；不连接模型或网络。

## 5. 0.8.2 Headless App Server与Agent SDK详细设计

### 5.1 模块边界

`harnessix.app_server`包含连接协议状态、应用服务和stdio传输；`harnessix.sdk.agent_client`包含公共异步SDK及进程内/子进程传输。应用服务只能调用`AgentRuntime`、`SessionStore`和`ProtocolRequestStore`，不能直接构造内部事件或执行Tool。SDK只能消费Agent Protocol公共模型，不能打开Session数据库。

### 5.2 受理与执行顺序

开始Turn和重试Turn先以`clientInstanceId + requestId`在协议账本提交`accepted`，再由Runtime以确定性UUID提交`TurnStarted`及初始用户Item，随后保存有界公共`TurnResult`，最后创建后台驱动任务。服务按`turnId`去重任务；Runtime以活动Turn再次拒绝并发执行。

Agent Event/Thread v18通过`executionMode`区分进程内立即驱动与App Server延迟驱动。只有延迟驱动的`ACCEPTED`尚未调用Provider或Tool，Runtime重开时保留该安全边界；旧`run_turn`在接受后异常退出仍按原语义中断。客户端可重发原Command，或调用`thread/resume`重新调度。Session migration21不改写历史事件和投影，旧记录读取时默认`immediate`。已进入模型、工具、审批或Process边界的恢复继续完全服从既有Agent Runtime语义，不由App Server猜测。

### 5.3 stdio与背压

- 输入输出均为UTF-8 JSONL，一行一帧；
- 单Reader顺序验证请求，单Writer串行写stdout；
- 初始化协商后使用双方消息、Replay和出站上限的较小值；
- 出站队列在配置时间内不能进入容量窗口时结束连接，不继续读取并接受无限命令；
- Writer错误结束连接并向进程入口传播，不把诊断写入stdout；
- EOF进入closing，后台Turn最多等待宽限期，超时取消并由Runtime结算。

### 5.4 SDK恢复合同

`AgentClient`覆盖Thread创建、读取、列表、恢复、分叉、归档，Turn开始、重试、恢复、取消，审批答复和事件Replay。调用方负责持久保存`clientInstanceId`、每个Command的`requestId`及每个Thread最后消费的`scannedThrough`。JSON-RPC ID只在当前SDK实例递增，用于当前连接响应关联。

进程内传输用于嵌入和确定性测试；子进程传输通过argv直接启动，不使用Shell。Request读一个Response；Notification只写不读，避免等待协议明确禁止的响应。stderr只保存最近64 KiB诊断尾部，后续诊断包仍须执行Secret脱敏。

### 5.5 Workspace和本地身份

`workspace`必须是App Server宿主上的无NUL绝对路径。创建Thread只绑定路径，不证明目录存在，也不授予读写或执行权限；实际Action必须通过0.7的Workspace Snapshot、Execution Plan、Permission、Sandbox和Action Audit。

`clientInstanceId`是本地调用身份和幂等命名空间，不是远程认证凭据。0.8只支持父子进程stdio；未来开放套接字前必须另行设计操作系统主体绑定、认证和多租户隔离。

### 5.6 失败与恢复矩阵

| 切点 | 恢复行为 |
|---|---|
| 协议accepted前 | 客户端可用原requestId重试 |
| 协议accepted后、领域提交前 | 重跑确定性领域操作 |
| 领域提交后、协议completed前 | 返回同一Thread/Turn，不重复事实 |
| 协议completed后、后台调度前 | 重放或thread/resume调度同一ACCEPTED Turn |
| Response后、事件消费前断线 | 从最后scannedThrough继续Replay |
| stdout阻塞/失败 | 关闭连接，持久Session不受影响 |
| EOF或关闭时Turn仍运行 | 宽限期后取消并持久结算 |

### 5.7 当前限制

0.8.2只交付Snapshot与Replay恢复，不广告实时通知和服务端Request；流式Delta、审批/提问主动呈现、运行中Steering和薄CLI属于0.8.3。Artifact分页合同尚未绑定安全读取端口，因此服务端不广告`artifact/read`，不能因Schema存在而宣称可用。

## 6. 0.8.3 薄CLI与双向交互详细设计

### 6.1 交互边界

0.8.3使用“客户端Command + `events/next` Pull-Live”完成持续交互，不开启服务端发起的JSON-RPC Request。命令方向包括`turn/steer`、`approval/respond`、`question/respond`和`turn/cancel`；服务端方向由`events/next`返回持久事件、实时文本Delta、缺口和超时状态。`InitializeResult.capabilities.serverRequests`仍为空，客户端不得等待未协商的反向Request。

`events/next`结果结构为：

- `replay`：权威持久事件页，`scannedThrough`是唯一恢复游标；
- `deltas`：live-only文本增量，不参与Session重放；
- `liveHasMore`：当前内存缓冲仍有未读取Delta；
- `liveGap`：最旧Delta曾因1000条上限被丢弃；
- `timedOut`：等待窗口内没有持久事件或已协商Delta。

`timedOut=true`不能同时携带事件、Replay后续页、Delta、`liveHasMore`或`liveGap`。服务端先返回持久Replay；客户端收到`liveGap`后停止拼接相关Item的Delta，等待`item_finished`恢复完整文本。未声明`ClientCapabilities.itemDeltas`的连接只收到Replay和超时，不消费Delta。

### 6.2 stdio并发与SDK响应归并

握手阶段仍按输入顺序同步处理，确保`initialize`响应和`notifications/initialized`先于领域请求生效。进入`ready`后，单Reader最多创建`maxPendingRequests`个处理任务；所有响应进入同一有界Outbox，由唯一Writer写stdout。请求完成顺序不构成协议顺序。

`SubprocessAgentTransport`包含启动锁、写锁、关闭锁、唯一Response Reader和`id → Future`待决表。`exchange`在写帧前登记ID，Reader解析每一行并只结算匹配Future。重复未决ID、未知Response ID、非法JSON、EOF、启动失败和输入流失败均转换为稳定`AgentSDKError`。调用协程取消后，其ID进入迟到响应集合，迟到Response只被丢弃一次，不得错误结算后续请求。AgentClient每次调用保存局部JSON-RPC ID，不能用之后已经递增的全局序号校验较早响应。

该结构保证同一连接上的30秒`events/next`不会阻塞审批、提问答复、取消或Steering。EOF关闭顺序为：App Server进入closing并唤醒长轮询、收敛已接受请求、停止Writer、SDK等待子进程；超过界限才逐级terminate/kill。

### 6.3 持久提问状态机

`AgentRuntime(enable_questions=True)`注册内建`ask_user`只读Tool。输入合同包含1～4000字符问题和最多8个唯一选项；每个选项1～500字符。该Tool不并行、不需要审批、不执行外部副作用。

```text
EXECUTING_TOOLS
  ├─ 参数非法 → failed ToolResult → 继续模型循环
  └─ 参数有效 → QuestionRequest完成 + WAITING_INPUT
                                      │
                    question/respond  │ 同一事务
                                      ▼
                 QuestionAnswer完成 + EXECUTING_TOOLS
                         + succeeded ToolResult
                                      │
                              下一模型步骤
```

Question ID、Question Item ID、Answer Item ID和Result Item ID均由原Tool Call稳定推导。Reducer要求提问来自当前首个待决`ask_user` Call；回答必须绑定同一Question和Call；离开`WAITING_INPUT`前必须已有唯一Answer；Turn终结前必须生成配对Tool Result。回答Item用于客户端审计，不进入Provider历史；Provider只看到原Tool Call和`{"answer": ...}` Tool Result，避免供应商消息格式失配。

相同回答不追加事件，不同回答返回`question_conflict`；取消会结算待决只读Call并关闭问题；迟到回答为`question_closed`。等待时间计入原Turn墙钟预算，过期回答为`question_expired`，显式恢复或Runtime重开把Turn结算为`time_budget_exceeded`。崩溃发生在问题事务后时保持`WAITING_INPUT`；发生在回答事务后、后台调度前时，`EXECUTING_TOOLS`只有在配对成功Tool Result存在时才允许继续。

### 6.4 Steering顺序与竞态

`turn/steer`必须携带当前`threadId`、`turnId`、领域`requestId`及正文。正文以稳定Item ID持久化为`user_message`。允许状态为`ACCEPTED`、`PREPARING_CONTEXT`、`CALLING_MODEL`、`EXECUTING_TOOLS`、`WAITING_APPROVAL`、`WAITING_ACTION`和`WAITING_INPUT`；`FINALIZING`、取消中和终态返回`steering_closed`。

Steering不取消当前Provider调用，也不插入当前请求已经冻结的历史。模型步骤结束时，Runtime在同一Thread锁内重新读取Session：存在未被当前请求消费的用户Item则提交`PREPARING_CONTEXT(reason=steering)`，否则提交`FINALIZING`。如果Steering早于本步骤首个模型Item到达，Reducer根据本步骤`ModelHistoryInspection`把模型输出、Tool Call和Tool Result插在待决Steering之前，使下一次Provider历史保持“旧请求历史 → 本步骤完整响应 → Steering”。`context_overflow`和`steering`使用不同持久reason，禁止互相冒充恢复条件。

### 6.5 Scoped Artifact与Diff审批

只有宿主显式提供`ScopedProtocolArtifactReader`时，初始化能力和方法集合才包含`artifact/read`。Reader不接受客户端Workspace Scope，而是读取Thread绑定的Workspace，经`ArtifactAccessScope`重新取得当前能力，再使用Session归属、Workspace Scope和Artifact ID执行最多200条、24 KiB的分页读取。错误继续使用`artifact_not_found`、`artifact_expired`、`artifact_corrupt`或Workspace能力错误，不暴露数据库路径。

薄CLI发现未决审批后，先读取并显示`diffArtifact`全部页面，再询问批准或拒绝。真实整组Patch测试验证展示的是`view=plan`的审批前Diff，而不是效果完成后的报告；批准仍通过`approval/respond`进入既有Patch审批、CAS和事务执行路径。

### 6.6 薄CLI命令与恢复

`harnessix agent`只构造`SubprocessAgentTransport`和`AgentClient`，服务程序及参数使用argv传入，不经过Shell。当前命令包括：

| 命令 | 行为 |
|---|---|
| `create`、`list` | 创建和列出Thread |
| `run` | 开始Turn并持续跟随 |
| `follow`、`resume` | 调用`thread/resume`后从Replay恢复并继续交互 |
| `retry` | 从最新失败/取消/中断Turn创建正式Retry并跟随 |
| `fork`、`archive` | 无授权复制历史或归档空闲Thread |
| `steer`、`cancel` | 向指定活动Turn补充输入或取消 |

CLI每次跟随先从游标0重建公开Item投影，以恢复待决审批和提问，再只显示`displayAfter`之后的事件。快速完成导致客户端未收到Delta时，完成的assistant Item提供最终文本；已完整显示Delta时，Item完成只补换行；出现`liveGap`时丢弃后续相关Delta并等待完整Item。CLI在本进程内记录已经成功提交的审批/问题ID，避免响应到达但后台状态尚未推进时重复提示；跨进程恢复依赖持久Answer或Approval Item。

### 6.7 版本与兼容

Agent Event/Thread当前版本升级为v19，Session migration22把快照投影版本升级为19。v18及更早事件不能承载Question、`WAITING_INPUT`或非`normal`状态原因。协议v1在0.8预发布阶段增加`events/next`、问题内容和交互参数Schema；旧客户端按既定规则忽略未知输出字段及未订阅能力。

`WAITING_INPUT`会改变Turn状态枚举。为保持已发布Cost Report v1/v2、Smoke和Campaign Schema哈希，旧合同引用冻结的v18状态集合；等待输入状态生成`harnessix.cost-report/v3`，v3统一允许无Compaction与有Compaction账本。历史`thread-fork-v1`继续作为冻结Schema，不由当前包含Question的内部联合重新生成。

### 6.8 0.8.3验收

- Question等待、重启、回答、重复/冲突、取消、过期、非法参数和硬退出；
- Steering运行期、`ACCEPTED`窗口、首Item前竞态、关闭状态和Provider历史顺序；
- `events/next` Replay、Delta、能力协商、超时、溢出缺口和断连；
- stdio乱序Response、多路复用、malformed Response、慢Writer和EOF；
- 薄CLI快速完成回放、编号提问、恢复，以及真实整组Patch Diff读取后审批；
- v19/v3 Schema、migration22、升级探针和全部历史Schema哈希。

本地完整门禁为3208 passed、12 skipped，293.20秒；Ruff格式与规则检查636个文件通过，Mypy严格检查228个源文件通过。186份Schema生成物聚合SHA256为`bd15e7dcfc6c775bcaa02b39dfa373f19881ff05c268595e7f1a308cea3092dd`，migration22 SHA256为`63e4fa0983de87e2d6bc5c8e4a5bbc126c6de6351c0abec99aacacacc808a0b1`。测试只使用本地确定性Provider、临时SQLite和受管副本，不调用模型API或远程服务。

独立wheel升级使用历史提交`e0e8498`创建真实v8 Session；当前wheel仅追加migration10～22，保持旧事件和投影原字节，旧reader明确拒绝新库，当前v19继续写入并保持Replay一致。

## 7. 0.8.4 MCP详细设计

### 7.1 模块与信任边界

`harnessix.mcp`由五个边界组成：

| 模块 | 职责 | 不拥有的能力 |
|---|---|---|
| `contracts` | 服务端身份、Tool快照、目录快照、连接事件/投影和调用输出合同 | 不执行Tool，不解释信任 |
| `schema` | JSON Schema 2020-12、参数和结果的有界验证 | 不授予Permission |
| `store` | 私有SQLite目录快照、哈希链连接事件和重启恢复 | 不保存Secret或Tool结果正文 |
| `runtime` | 官方MCP SDK Client、stdio进程生命周期、目录刷新和调用 | 不直接访问Agent Session或审批 |
| `actions`、`server` | MCP Client到可信Action的适配，以及可选低风险只读MCP Server | 不持有Router、Executor Registry或Secret Provider |

第三方MCP Server、其二进制、描述、Annotation、Schema和结果均按不受信输入处理。生产本地Client只接受`McpContainerStdioTarget`：启动argv必须来自`ContainerCommandBuilder`，并同时绑定不可变镜像、`ContainerExecutionSpec`、强Sandbox Profile、网络模式和进程身份。`McpInProcessTarget`只用于受信宿主内嵌与测试，不加载第三方Python模块。任意远端URL、Header、OAuth和Streamable HTTP配置进入0.9.4独立目标身份、Secret生命周期与受管出口切片，不复用0.8.6模型Provider认证。

### 7.2 协议代际与连接状态

Client使用官方`mcp` Python SDK 2.x的`mode="auto"`：优先探测2026代`server/discover`，并兼容仍使用`initialize/initialized`的旧服务端。Harnessix不自行复制协议Codec。协商出的协议版本、服务端报告身份、Capability摘要、目标实现摘要和传输类型共同进入`McpServerIdentity`；报告名称和版本仅用于诊断，不能替代宿主分配的`serverId`。

连接状态机为：

```text
不存在 → connecting → connected → schema_changed → closed
                   └──────────────→ failed ─────────→ closed
failed / closed / schema_changed → connecting
```

每次转换追加带前序摘要的`McpConnectionEvent`，并在同一SQLite事务更新当前投影。目录以`serverId + generation`不可变保存；语义相同的重连仍生成新代次，目录摘要故意排除捕获时间和代次。宿主重开时，遗留`connecting/connected`只收敛为`failed(mcp_host_interrupted)`，不声称远端进程或Tool仍可执行。

### 7.3 Tool目录与Schema绑定

`tools/list`最多读取1000页、2048个Tool；重复Cursor、重复原始名称、超限定义和非法字段使整个连接失败关闭。模型名称固定为`mcp__<server>__<tool>`；规范化冲突或超长名称追加原始名称SHA-256前缀，原始名称始终单独保存并用于协议调用。

每个`McpToolSnapshot`保存原始/模型名称、输入/输出Schema、显示字段、Annotation、原始定义摘要和完整Tool摘要。Description与Annotation仅进入不受信目录快照，绝不决定`EffectClass`、`RiskLevel`、Permission、Recovery或Sandbox。输入Schema必须以object为根，使用JSON Schema 2020-12；禁止外部`$ref`、`$id`、`pattern`和`patternProperties`，并限制字节、深度、节点和字符串长度。调用参数在规划时依照捕获Schema验证，非法或无法解析的本地引用统一为`tool_invalid_arguments`。

每次调用在同一服务端锁内先以`cache_mode="bypass"`完整刷新目录。当前目录或目标Tool摘要与计划绑定不一致时，先持久化`schema_changed`，再以`mcp_tool_schema_changed`拒绝，实际`tools/call`尚未发送。列表变化通知只可作为刷新提示，不能代替调用前权威检查。

### 7.4 可信Action路由与失败语义

宿主必须为每个准入Tool提供`McpTrustedToolPolicy`，显式声明原始名称、效果类别、风险、资源解析和恢复方式。动态MCP Schema通过`TrustedActionDefinition.input_schema + decode_arguments`进入既有Router，注册摘要、计划、审批、Workspace、Sandbox和执行器身份仍由0.7.5合同冻结。模型侧只获得已在对应`ExtensionActionPort(source="mcp", sourceId=serverId)`注册且指纹仍匹配当前目录的Tool。

写Tool必须声明`external_reconcile`并提供只观察外部事实的Reconciler；只读Tool必须使用`recovery_mode="none"`。结果在跨越Action边界前限制为1 MiB并执行Secret脱敏。调用失败按“是否可能已经发送”区分：

| 切点 | 只读Tool | 写Tool |
|---|---|---|
| 参数、计划、Sandbox或Schema漂移，调用前拒绝 | `failed` | `failed` |
| 调用后超时、断连、结果非法、`input_required` | `failed` | `unknown` |
| 服务端返回`isError=true` | `failed` | `unknown` |
| 调用协程取消 | Action Router持久`failed(executor_cancelled)` | Action Router持久`unknown(cancelled_write_effect_unknown)` |
| 重开后存在`running/reconciling` | 按Router恢复规则收敛 | 只允许显式Reconcile，不重放原调用 |

保守地把写Tool的`isError`视为UNKNOWN，是因为协议错误结果不能证明外部效果尚未发生。MCP的多轮工具请求`input_required`尚未映射为持久Agent交互；当前版本明确失败，不把SDK内存回调伪装为可恢复审批。

### 7.5 进程关闭与可选Server

stdio Client的启动、目录发现、调用和关闭均有独立超时。SDK退出后还必须调用`ContainerCommandBuilder.cleanup_container`，以进程ID、执行摘要、容器名和双标签证明容器不存在；无法证明时持久化并返回`mcp_process_cleanup_failed`。真实子进程故障测试覆盖服务端硬退出、调用超时和关闭后进程消失；固定摘要BusyBox容器门禁额外覆盖禁网、只读Workspace、低资源限制、协议调用和无残留关闭。

可选`HarnessixMcpServer`只导出宿主显式白名单中的低风险、只读、无需恢复且Schema摘要一致的`TrustedToolBinding`。列表和调用都重新核对当前绑定；调用仍通过`ExtensionActionPort.plan/execute`，非`ready`计划只返回安全错误，不代表MCP客户端拥有批准权。Server只提供本地stdio入口，不开放网络监听。

### 7.6 0.8.4验收

- 目录分页、重复Cursor、名称冲突、恶意描述/Annotation、非法/超限Schema和漂移均失败关闭；
- SQLite重开、事件链、相同目录多代次、并发连接、损坏正文和中断恢复均有确定结果；
- 只读调用、写审批、写超时UNKNOWN、显式Reconcile、取消、Secret结果脱敏和动态Schema接入统一Action Plane；
- 可选Server只导出显式低风险只读Action，写绑定、缺失Tool和非法参数被拒绝；
- 真实stdio子进程覆盖Crash/Timeout/进程清理，真实固定镜像Container覆盖强Sandbox绑定；
- 六份MCP JSON Schema由运行时合同生成并逐项比对，官方SDK许可证进入第三方通知。

0.8.4不增加Agent Protocol方法或Session migration。MCP连接与目录使用独立私有数据库；调用审计和UNKNOWN恢复仍使用0.7.5 Execution Plan/Action Audit事实。

本地完整门禁为3235 passed、13 skipped，319.39秒；Ruff格式与规则检查655个文件通过，Mypy严格检查236个源文件通过。192份Schema生成物按文件名、NUL和原字节聚合SHA256为`4231d529343624f8c4a963e8d71c991303b65e995c5b4cf3b8f4602e2e3a25ce`。13项跳过只包含平台限定或本机未配置的集成场景；真实Container MCP由固定镜像CI门禁提供发布证据。

## 8. 0.8.5 Skills与Hooks详细设计

### 8.1 模块与执行边界

`harnessix.skills`由合同、目录运行时、SQLite Store和Action适配组成；
`harnessix.hooks`由定义/运行合同、SQLite Store和生命周期运行时组成。Skill是
不受信内容包，不是可执行插件；Hook定义是受信宿主对既有只读Action的声明式绑定，
不是Shell、HTTP、Prompt或进程内回调。两者均不得持有Router、Executor、Session、
Secret Provider或任意文件系统对象。

Skill正文与资源分别注册为`skill.load`和`skill.read_resource`，固定为
`READ_ONLY + LOW + recovery=none`并只经`ExtensionActionPort(source="skill")`
计划和执行。Hook处理器必须是宿主预注册的
`ExtensionActionPort(source="hook")`绑定，且同样只能为低风险只读Action。
目标Action自己的Policy、Approval、Sandbox和恢复结论保持权威；Hook的`allow`不能
放宽目标Action，`before_action`的`deny`或失败只能收紧执行。

### 8.2 Skill来源、目录与冲突

来源只接受宿主显式绑定的`bundled`、`user`和`workspace`本地Root。初始化使用
POSIX目录描述符/no-follow或Windows目录句柄/Reparse Point检查绑定Root身份；Root
本身为链接、Junction或重解析点时失败关闭。发现过程限制32个来源、2048个Skill、
2048个目录、8192个条目、6层目录和有界Frontmatter，不跟随链接或特殊文件。

`SKILL.md`只解析安全YAML中的`name`、`description`和可选`version`。重复键、Alias
扩张超限、非UTF-8、NUL、过深结构、空正文或超限正文形成稳定发现问题，不使整个
宿主读取任意内容。Frontmatter中的Tool、Hook、Shell、模型或权限字段没有授权含义。

每次发现生成不可变`SkillCatalogSnapshot`和来源/Manifest摘要。来源内相同限定名称
全部排除；跨来源同名保留但建立冲突索引，普通名称只能在全局唯一时解析，否则必须
使用`source/name`。语义相同的重新发现可以形成新代次，但目录内容摘要保持相同，
便于解释实际装载版本而不把时间戳伪装成内容变化。

### 8.3 渐进加载与资源安全

目录只向模型公开名称、描述、来源、版本、路径摘要和内容摘要，不持久化正文。调用方
必须同时提交目录摘要和预期Manifest摘要；加载时重新验证目录、Root对象身份、文件
类型和内容摘要。目录形成后正文变化以`skill_content_changed`失败，不自动采用新内容。

资源只允许Skill自身目录下的规范相对路径，单个资源最大64 KiB且必须为UTF-8普通
文件。符号链接、Windows Reparse Point、硬链接、特殊文件、绝对/回退/反斜线路径、
敏感名称以及嵌套Skill均不可作为资源读取。目录最多列出64个资源；读取前后分别核对
句柄身份、大小和内容，关闭检查到使用之间的路径替换窗口。

`SQLiteSkillStore`使用WAL和`FULL`同步保存不可变目录代次以及哈希链访问事件，仅记录
操作、Manifest/资源路径/结果摘要和稳定错误码，不保存绝对Root、正文、资源内容或
Secret。读取输出在进入Action结果前通过`SecretLeakGuard`；发现内容中出现已保护
凭据不会被持久化，尝试输出时失败关闭。

### 8.4 Hook定义、授权与匹配

Hook事件固定为`session_started`、`session_ended`、`turn_started`、
`turn_completed`、`before_action`和`after_action`。只有Action事件允许Matcher；Matcher
只接受来源、来源身份和Tool的精确值或单字段`*`，不接受正则。执行顺序固定为
`event + order + qualifiedId`，并在同一Dispatch内串行执行。

Bundled Hook由发行物信任；`managed`、`user`和`workspace` Hook必须提供绑定完整
`definition_sha256`的未过期`HookTrustGrant`。事件、Matcher、顺序、模式、超时、
Action版本、Schema或指纹任一变化都会使旧授权失效。Registry初始化还会复核处理器
真实绑定为`source="hook"`、低风险只读、无需恢复，并且输入Schema与
`HookActionInput`精确相等。

### 8.5 输入最小化、阻断与失败语义

Hook输入只包含Registry/Definition/Dispatch身份、Thread/Turn ID、目标Action来源和
Tool、目标Plan ID，以及参数或结果摘要。来源身份在进入处理器前再摘要化；原始参数、
Action输出、模型正文、路径正文、环境和Secret都不进入Hook输入。输出只接受
`allow`或带稳定`reason_code`的`deny`，并经过严格Schema、尺寸和Secret检查。

| 事件 | 模式 | 失败策略 | 对目标流程的影响 |
|---|---|---|---|
| `before_action` | blocking | fail closed | `deny`、超时、取消、计划/执行/输出失败均阻止目标Action |
| 其他事件 | advisory | record only | 记录成功或失败，不改变已经形成的目标事实 |

Advisory Hook返回`deny`属于非法输出并记录失败，不能追溯撤销Turn或Action。多个
Blocking Hook遇到首个非成功结果立即停止后续执行。相同Dispatch和定义使用确定性
UUID形成同一Run；已存在终态直接返回，不重复调用处理器。

### 8.6 持久化、取消与恢复

`SQLiteHookStore`保存不可变Registry快照、Run Plan、当前投影和哈希链Run Event，
状态机为`ready → running → succeeded|failed|blocked|cancelled`；进程重开把遗留
`running`收敛为`interrupted`。Store核对合法转换、Plan/事件/投影摘要和序列，检测到
正文或链篡改时失败关闭。

每个定义具有100毫秒至60秒独立超时。超时和调用方取消都会取消底层只读Trusted
Action；Action Audit据此结算为`failed(executor_cancelled)`，Hook分别持久化
`hook_timeout`或`hook_cancelled`。Interrupted Run不自动重放；生命周期调用方必须
产生新的Dispatch才能显式重试，避免把已经观察过的Hook执行伪装成从未发生。

### 8.7 跨平台读取实现

0.8.5从既有Workspace安全路径能力提取公共`SecureWorkspaceReader`。POSIX复用目录
描述符、`O_NOFOLLOW`、普通文件/链接计数和双次目录观察；Windows复用
`WindowsWorkspaceRoot`句柄链、大小写规范化和Reparse Point检查。该抽取不改变原
Workspace Snapshot合同，只允许Skill运行时在同一安全语义下进行有界目录和文件读取。

### 8.8 0.8.5验收

- Skill覆盖同目录重复、跨来源冲突、内容漂移、Root/资源链接、POSIX硬链接、敏感路径、
  二进制、YAML重复键/Alias超限、目录重开与访问链篡改；
- Skill Action覆盖目录指纹绑定、正文/资源渐进加载、跨来源端口隔离和Secret canary；
- Hook覆盖精确授权、过期/定义漂移、Binding/Schema/effect错配、Matcher、顺序、阻断、
  Advisory、目标Policy不可放宽、超时、取消、重复Dispatch和输出泄漏；
- 独立崩溃进程在`running`状态硬退出，重开只恢复为`interrupted`且不重放；
- 21份Skill/Hook JSON Schema由运行时合同生成并逐项比对；PyYAML许可证进入第三方通知；
  macOS和Windows CI显式运行两套回归。

专项确定性回归为29 passed，Ruff和Mypy严格检查245个源文件通过。Schema目录共213份
JSON文件，按文件名、NUL和原字节聚合SHA256为
`0c25f173c7ad4ad1c205e45cc872fa81fd8985de62e37b225b8ecbb6839de552`。验证不调用模型
API、远程服务、SSH或用户服务器。完整仓库门禁和跨平台CI证据在本切片提交后记录；
在CI关闭前只称为本地验收完成。

## 9. 0.8.6 Provider与配置产品化详细设计

### 9.1 模块边界

`harnessix.product_config`是产品配置域，不取代0.4的Provider Adapter或0.6的Agent状态机：

| 模块 | 职责 | 明确不拥有 |
| --- | --- | --- |
| `contracts` | v1/v2配置、Profile选择、诊断、迁移、配置审计和Fallback决策合同 | Secret值、HTTP Client |
| `codec` | 有界严格JSON及跨平台安全文件读取 | include、模板替换、远端配置 |
| `migration` | v1到v2的CAS、备份、原子替换和收据 | 自动回滚已发布v2 |
| `store` | 不含明文的快照、活动指针和两条Hash事件链 | Session、Provider响应正文 |
| `runtime` | Profile选择、离线诊断、Secret解析、Provider构造和安全Fallback | Tool执行、Turn恢复 |
| `server` | 固定Workspace的产品装配和stdio生命周期 | 第二套Agent Loop、网络监听 |
| `cli` | `diagnose`、`migrate`和`agent-server`进程入口 | 交互式配置编辑器、TUI |

依赖方向固定为`CLI/Server → Product Config → Model/Secret/Session/App Server`。Model Adapter只
新增显式`api_key`构造参数，并保留原环境引用入口兼容既有宿主；产品配置域不读取Adapter的
环境变量名，也不把Secret值复制到Pydantic合同。

### 9.2 配置合同

唯一正式输入格式是最大256 KiB的UTF-8 JSON。`ProductConfigV2`由三组规范排序数组组成：

1. `secret_sources`：`SecretReference(name, version)`到受信环境变量名的白名单映射；
2. `providers`：稳定Provider ID、`openai_chat|anthropic`类型、HTTPS端点、Secret引用和可选
   OpenAI输出Token参数；
3. `profiles`：稳定Profile ID、Provider引用、精确模型、声明/要求能力、请求边界、Adapter
   内部尝试数和显式有序Fallback列表。

加载同时拒绝未知字段、重复键、非有限数、标量类型转换、非法UTF-8/NUL、深度超过32、节点
超过20000、未排序或重复ID、一个环境变量映射多个Secret引用、悬空引用、Secret版本不一致、能力不足、Fallback环/重复展开、
超过六个候选及累计超过32次模型尝试。配置对象冻结；规范配置摘要来自领域值，源摘要来自
实际文件字节，两者分别用于语义身份和迁移CAS。

`ProductConfigV1`只作为迁移输入，Provider中的`api_key_env`迁移为名称
`<provider-id>-api-key`、版本`env-v1`的Environment Secret Source；多个Provider共用同一旧环境
变量时复用按Provider规范顺序首次建立的Secret引用。运行入口拒绝直接启动v1。
配置示例位于[`docs/examples/product-config-v2.json`](examples/product-config-v2.json)，示例
端点和模型是占位值，不包含凭据。

### 9.3 安全读取与迁移

配置文件使用`SecureWorkspaceReader`。POSIX要求当前用户拥有的普通文件、无组/其他权限、
单硬链接，并通过目录描述符和`O_NOFOLLOW`读取；Windows复用句柄链、大小写规范化和Reparse
Point检查。读取前后身份、大小和时间观测变化时返回`product_config_changed`，不使用首次
读取正文继续启动。

迁移要求调用方提交完整源SHA256。单进程流程为：

```text
私有迁移锁 → 安全读取 → 源摘要CAS → v1全量校验 → v2内存转换与全量校验
→ 同目录临时文件0600 + fsync → 私有源备份 + 目录fsync
→ 锁内再次核对源摘要 → os.replace → 目录fsync → 迁移收据
```

替换前退出不会改变源文件；替换后、收据返回前退出时，重开把现有v2识别为幂等完成，不再次
改写。备份冲突、非协作写入或锁竞争均失败关闭。`ConfigMigrationReceipt`绑定源、目标和备份
摘要；配置数据库启用时再把收据摘要写入配置审计链。

### 9.4 Profile选择与离线诊断

`select_profile`从活动Profile或显式ID生成不可变`ProfileSelection`，绑定配置摘要、按深度优先
展开的Profile/Provider/模型链及选择摘要。诊断和Provider构造会重新从配置生成选择并逐字段
比对，拒绝调用方伪造、截短或重排候选链。

`diagnose_configuration`不连接网络，只检查：配置合同、每个候选对首选能力要求的满足情况、
对应Provider SDK可导入性，以及Secret名称/精确版本/8 KiB可打印ASCII格式。报告按
`scope + subject_id + code`排序并绑定摘要，只包含标识、枚举和通过状态，不包含端点、环境值、
Secret正文或底层异常。`ready=false`时`agent-server`不会构造Provider或打开stdio。

### 9.5 Provider构造与Secret生命周期

Environment Secret Provider只解析配置列出的变量，不枚举环境。每个候选构造前解析
`SecretReference`并再次核对名称和版本；值必须是不超过8 KiB的非空可打印ASCII且不含空格。短生命周期
`bytearray`在工厂返回后清零，Adapter持有SDK所需的字符串副本直到Client关闭。两个Adapter
均禁用环境代理、重定向、SDK自动重试和供应商自定义Header环境变量。

候选构造采用全有或全无：任一Secret或工厂失败时，逆序尽力关闭全部已构造Client，并保留
原始构造错误。Bundle关闭同样尝试关闭所有候选，即使某个Client关闭失败也不会跳过其他候选。

### 9.6 Fallback状态机

每个Adapter先按Profile自身`max_attempts`完成同Provider重试；`SafeFallbackProvider`只在候选
最终发出`ResponseFailed`时决定是否进入下一Profile：

```text
候选失败
  ├─ 非 transport/rate_limit/provider_internal，或 retryable=false → 原失败
  ├─ 已暴露任意响应事件 → 原失败
  ├─ 无下一候选或无配置审计Store → 原失败
  ├─ Fallback审计提交失败 → 原失败
  └─ 零暴露 + 可重试 + 审计成功 → 抑制中间失败，启动下一候选
```

只有`ModelAttemptStarted`、`ModelUsageObserved`和`ModelAttemptFinished`属于允许切换的内部元数据；
`ResponseStarted`、文本、Tool Call、完成事件以及未来新增的未知Provider事件均关闭Fallback窗口。
因此已知费用和失败尝试仍进入Session，但不会重复已经展示的输出或可能触发的Tool Call。

编排器把各Adapter局部尝试号重写为同一步骤内1～32的全局连续序号，把Provider字段重写为
配置Provider ID；不重写Attempt ID、请求历史或供应商私有响应。Fallback是新的完整请求，不是
旧流续传。

### 9.7 持久化与配置切换

`SQLiteProductConfigStore`使用WAL、`synchronous=FULL`、外键和STRICT表，POSIX数据库为0600、
父目录为0700；既有共享目录、链接或多硬链接数据库会被拒绝，不通过自动`chmod`改变调用方目录。
数据包括：

- `product_config_snapshots`：以规范配置摘要为主键的无Secret快照；
- `product_config_active`：唯一活动配置摘要和Profile；
- `product_config_events/head`：`loaded|activated|migrated`连续Hash链；
- `provider_fallback_events/head`：零暴露Fallback决策连续Hash链。

读取事件时重算正文摘要、前驱摘要、连续序号和Head；任一不一致返回
`product_config_store_corrupt`。活动切换要求`expected_active_sha256`和
`expected_active_profile`组成的旧指针与当前值同时一致；首次激活两者都要求`null`，相同
配置/Profile重复激活幂等，其他竞争返回`product_config_conflict`，因此同一配置内的并发
Profile切换也不能丢失更新。激活事件同时保存旧摘要和旧Profile。

产品启动顺序固定为：安全加载→选择→固定Workspace并拒绝配置重叠→离线诊断→固定状态根/Git可执行文件→保存
快照→构造全部Provider→初始化Session并进入Bundle/Tool/Agent Runtime生命周期→CAS激活→
开放stdio。组件初始化或CAS失败会逆序关闭已进入的生命周期且不开放协议；新配置只影响新
进程，活动Turn不热换流。

### 9.8 产品入口和部署

```bash
# 从仓库示例创建私有配置；先替换占位端点和精确模型
cp docs/examples/product-config-v2.json "$HOME/.harnessix/product-config.json"
chmod 600 "$HOME/.harnessix/product-config.json"   # POSIX

# 值只进入受信进程环境，配置仅保存变量名和版本声明
export HARNESSIX_PRIMARY_API_KEY='***'
export HARNESSIX_BACKUP_API_KEY='***'

uv run harnessix config diagnose \
  --config "$HOME/.harnessix/product-config.json" \
  --state-database "$HOME/.harnessix/config-audit.db"

uv run harnessix agent-server \
  --config "$HOME/.harnessix/product-config.json" \
  --workspace /absolute/project \
  --state-directory "$HOME/.harnessix/runtime/project-id"
```

`agent-server`只注册固定Workspace的现有只读Coding Tool Runtime；客户端创建Thread时提交其他
或不存在的Workspace会失败。该Tool Runtime当前仅支持macOS/Linux；Windows产品入口在创建
状态或Provider前以`product_tools_platform_unsupported`失败关闭，Windows完整产品Tool装配仍
由0.9交付。配置文件必须位于Workspace之外，状态目录不得与Workspace互相包含，stdout专用于stdio JSONL，
诊断只写stderr。写工具、Sandbox产品装配、TUI、安装器和自动更新仍属于0.9发布切片。

### 9.9 失败语义与恢复

| 错误类别 | 是否自动重试 | 恢复动作 |
| --- | --- | --- |
| 配置格式/权限/引用/能力无效 | 否 | 修复配置并重新诊断 |
| Secret缺失或版本变化 | 否 | 恢复受信来源，或显式更新版本和配置摘要 |
| 依赖缺失 | 否 | 安装对应Provider可选依赖后重启 |
| 迁移CAS/锁/备份冲突 | 否 | 读取最新源摘要，核对备份后重新发起 |
| Provider构造失败 | 否 | 活动指针不变，修复配置或依赖后重启 |
| 活动配置CAS冲突 | 否 | 读取活动摘要，重新决定是否切换 |
| 零暴露可重试Provider失败 | 有条件 | 审计成功后只切换到显式下一候选 |
| 已暴露响应后的Provider失败 | 否 | 保留失败和已知用量，由用户发起新Turn/Retry |
| 配置/Fallback事件链损坏 | 否 | 停止使用数据库，从一致备份恢复并审计 |

### 9.10 安全边界与非目标

- 配置、Schema、Session、配置数据库、CLI输出和测试均不得包含Secret值；
- Environment版本是部署者声明，不等价于云Secret Manager的强版本证明；
- SQLite Hash链用于检测意外或越界修改，不是抵御同用户恶意进程的签名日志；
- 配置文件不支持热加载；轮换需要新版本引用、新摘要、诊断、CAS和进程重启；
- 0.8.6不开放远端MCP Streamable HTTP/OAuth/自定义Header，也不开放公网Git认证；二者分别
  进入0.9.4安全供应链和0.9.5跨平台Dogfooding门禁；
- 真实Provider能力与计价发布证据属于0.9.6，不以离线Mock或既有Eval结果代替。

### 9.11 验收范围

确定性测试覆盖严格JSON、路径/权限/链接、Profile图与能力、Secret错版、迁移CAS及替换前后
崩溃、配置/Fallback Hash链篡改、选择伪造、候选构造和关闭故障、零暴露切换、响应/Tool Call
暴露后禁止切换、审计失败、全局尝试序号、双Adapter显式Secret Mock传输、CLI诊断/迁移、
固定Workspace、启动/EOF关闭及构造失败不激活。正式Schema由同一Pydantic合同生成并逐项比对。
该验收不连接真实模型、远端MCP、Git远端或用户服务器。

0.8.6专项为49项通过；原生宿主全仓为3313项通过、13项按平台/本地集成条件跳过，Ruff、Mypy、
17个离线示例、构建及基础wheel安装通过。完整边界与证据见
[测试规范第78节](testing-and-evals.md#78-086-provider与产品配置候选验收2026-09-09)。远端六矩阵
未关闭前，0.8仍只标记为本地验收完成。

## 10. 参考资料

- [Agent Protocol与产品运行时源码研究](research/agent-protocol-product-runtime.md)
- [ADR 0009：JSON-RPC 2.0与stdio JSONL](adr/0009-app-server-protocol.md)
- [ADR 0070：Agent Protocol v1边界](adr/0070-agent-protocol-v1-boundaries.md)
- [ADR 0071：Headless App Server与Agent SDK生命周期](adr/0071-headless-app-server-and-sdk-lifecycle.md)
- [ADR 0072：持久交互与Pull-Live事件流](adr/0072-durable-interaction-and-pull-live-stream.md)
- [MCP运行时与安全源码研究](research/mcp-runtime-and-security.md)
- [ADR 0073：MCP目录绑定与Sandbox](adr/0073-mcp-catalog-binding-and-sandbox.md)
- [Skills、Hooks与供应链边界源码研究](research/skills-hooks-and-supply-chain.md)
- [ADR 0074：Skill快照与Hook Action安全边界](adr/0074-skill-snapshot-and-hook-action-boundary.md)
- [Provider、Profile、配置与安全Fallback源码研究](research/provider-profile-config-and-safe-fallback.md)
- [ADR 0075：Provider Profile、Secret引用与安全Fallback](adr/0075-provider-profile-secret-and-safe-fallback.md)
- [JSON-RPC 2.0规范](https://www.jsonrpc.org/specification)
