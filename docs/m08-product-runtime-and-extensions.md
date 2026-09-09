# Harnessix Code 0.8 产品运行时与扩展详细设计

## 1. 文档状态

- 适用版本：0.8；
- 当前状态：0.8.1～0.8.3已完成本地验收，0.8.4～0.8.6待前序切片关闭后实施；
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

## 7. 后续切片冻结入口

0.8.3只能依赖Agent SDK，不直接打开Session或Runtime；0.8.4～0.8.5只能调用`ExtensionActionPort`；0.8.6的配置只能保存Secret引用，不能把凭据值写入协议、Session或配置文件。具体设计在对应源码研究和ADR完成后追加。

## 8. 参考资料

- [Agent Protocol与产品运行时源码研究](research/agent-protocol-product-runtime.md)
- [ADR 0009：JSON-RPC 2.0与stdio JSONL](adr/0009-app-server-protocol.md)
- [ADR 0070：Agent Protocol v1边界](adr/0070-agent-protocol-v1-boundaries.md)
- [ADR 0071：Headless App Server与Agent SDK生命周期](adr/0071-headless-app-server-and-sdk-lifecycle.md)
- [ADR 0072：持久交互与Pull-Live事件流](adr/0072-durable-interaction-and-pull-live-stream.md)
- [JSON-RPC 2.0规范](https://www.jsonrpc.org/specification)
