---
doc_type: module-design
status: current
version: 8
code_revision: f11359447f3bc68ffb97a100bb8b4bbcc1a891e5
owners:
  - core
modules:
  - sdk
  - product_ui
related_adrs:
  - docs/adr/0001-python-first-runtime.md
  - docs/adr/0070-agent-protocol-v1-boundaries.md
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0072-durable-interaction-and-pull-live-stream.md
  - docs/adr/0078-product-shell-and-recoverable-client-state.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0089-bounded-local-transport-lifecycle.md
related_tests:
  - tests/app_server/test_server_sdk.py
  - tests/product_ui/test_recoverable_session.py
  - tests/app_server/test_agent_cli.py
  - tests/governance/test_product_runtime_convergence.py
supersedes: []
---

# SDK模块设计

## 1. 模块摘要

| 项目 | 内容 |
|---|---|
| 源码包 | [`src/harnessix/sdk`](../../src/harnessix/sdk/) |
| 当前职责 | 提供Agent Protocol v1异步客户端及进程内/子进程Transport |
| 非职责 | 不实现Agent状态机、协议Server、Action状态迁移、自动重连、游标持久化、认证、重试策略、CLI呈现或跨语言代码生成 |
| 兼容边界 | 旧`client.py`和`HarnessixClient/HarnessixAsyncClient`已物理删除；Agent Protocol是唯一SDK合同 |
| 上游调用者 | 薄Agent CLI、Product UI、Python宿主和测试 |
| 下游依赖 | Agent分支依赖Protocol公共合同；进程内Transport仅在类型检查时引用App Server |
| 持久化 | SDK不持久化任何状态；Client Instance ID、Command Request ID、Replay Cursor和Action ID均由调用方保存 |
| 连接 | Agent子进程Transport惰性启动一个stdio子进程，Response Reader具有构造期字节上限，活动与取消后未决Request共享容量 |
| 平台 | Python逻辑未设平台分支；子进程与HTTP机制可跨平台，但默认Agent产品Windows入口及三平台关闭证据尚未完成 |
| 公共导出 | `harnessix.sdk`导出`AgentClient`、两种Transport和`AgentSDKError`；根包不导出协议客户端 |
| 代码版本 | `f11359447f3bc68ffb97a100bb8b4bbcc1a891e5` |
| 当前完成度 | Agent主链、严格Response/Result、有界Frame、共享未决容量、迟到Response安全释放、取消安全Close、广告方法及协商消息/Replay上限前置门禁已实现；可恢复状态和连接代际由Product UI客户端内核提供；写入Timeout、进程树所有权和发布级平台证据仍缺失 |

本文是[`agent_client.py`](../../src/harnessix/sdk/agent_client.py)、[`subprocess.py`](../../src/harnessix/sdk/subprocess.py)、请求/响应辅助模块和
[`__init__.py`](../../src/harnessix/sdk/__init__.py)的当前事实源。公共JSON字段与兼容规则见
[Protocol模块设计](protocol.md)，服务端连接与stdio行为见[App Server模块设计](app-server.md)。
旧HTTP客户端删除决策见[ADR 0081](../adr/0081-single-coding-agent-product-boundary.md)。

## 2. 需求背景

Coding Agent客户端必须在不打开Session数据库、不调用Runtime内部方法的前提下完成Thread创建、Turn驱动、
审批、提问、事件消费和断线恢复。stdio允许本地产品以独立进程运行，但一旦多个请求并发，输入顺序不再
等于Response顺序；客户端必须按JSON-RPC ID归并，且调用取消不能让迟到Response错误结算另一个请求。

Harnessix早期Action Plane曾提供独立HTTP客户端。ADR 0081已撤销该客户端的产品地位：源码在迁移窗口内
暂时保留，包级公共入口不再导出，新增调用必须使用Agent Protocol或进程内Trusted Action合同。这样可以避免
把JSON-RPC Request ID、Turn状态与旧HTTP Action ID混为一谈，并确保产品只有一条恢复主链。

## 3. 设计目标、非目标与术语

### 3.1 当前设计目标

1. Agent调用只依赖公共Protocol Params/Result，不读取Server或Session私有对象；
2. Request和Notification使用不同Transport方法；
3. 子进程只通过argv启动，不经过Shell字符串解释；
4. 一个Response Reader按ID结算多个并发Future，允许乱序返回；
5. 一个Write Lock保证stdin JSONL帧不交错；
6. 调用协程取消后保留迟到Response身份，避免污染其他请求；
7. 初始化调用串行化并在同一Client实例中幂等返回结果；
8. 写命令的领域`requestId`由调用方显式提供，不从连接序号推导；
9. Replay恢复只推进`scannedThrough`，Live Delta不充当恢复游标；
10. Context Manager负责释放Transport；
11. 包级公共导出不得重新暴露旧Action HTTP客户端。

### 3.2 明确非目标

- 不自动发现、安装或升级App Server可执行文件；
- 不自动重启崩溃的子进程或迁移未决Request；
- 不持久保存`clientInstanceId`、Command `requestId`、Thread Cursor或Action ID；
- 不提供同步版Agent Protocol客户端；
- 不提供TypeScript、Java、Go或生成式OpenAPI/Protocol SDK；
- 不自动终止`watch_thread`，不根据Turn终态结束迭代；
- 不负责TUI布局、Diff渲染、Question选择或Approval交互；
- 不实现HTTP认证、Token刷新、代理策略、TLS Pinning或业务重试；
- 不保证自定义Transport与官方两个Transport具有相同取消语义；
- 不允许SDK绕过Server直接修改Session或Action Journal。

### 3.3 关键术语

| 术语 | 含义 |
|---|---|
| JSON-RPC ID | 单个AgentClient实例递增的连接级Response关联整数；不持久、不表示业务幂等 |
| Command Request ID | 调用方提供的领域命令身份；由Protocol Request Ledger跨重连持久化 |
| Client Instance ID | Protocol命令幂等命名空间；默认随机生成，调用方可注入并负责跨进程保存 |
| pending | 已写出或待写出且仍等待Response的`id → Future`映射 |
| abandoned | 调用协程已取消、但Server可能仍返回Response的ID集合 |
| sticky reader error | Reader首次协议/流错误；后续启动、写入和请求均失败，当前Transport不自恢复 |
| durable cursor | `EventsReplayResult.scannedThrough`；唯一可跨进程保存的事件消费进度 |
| 历史Action client | 已删除；只可在固定Git Revision研究，不属于当前包或公共API |

## 4. 当前能力与产品装配边界

| 能力 | 当前实现 | 默认产品使用 | 尚未实现 |
|---|---|---|---|
| Agent进程内Transport | 直接调用完整`AgentProtocolServer.process_frame` | 测试和嵌入使用 | 与子进程完全一致的取消隔离 |
| Agent子进程Transport | 惰性启动、并发归并、stderr尾部、有界退出等待 | 薄CLI使用 | 自动重启、进程树Owner、客户端队列上限 |
| Agent Client | 19个公开异步生命周期/资源方法，写入前校验广告方法、消息字节和Replay数量 | 薄CLI及Product UI内核使用 | 同步封装、并发/出站队列协商上限 |
| Replay与Delta | Replay/Next和无限`watch_thread` | CLI自行解释终态与交互 | 自动Gap修复、终态停止、重连续传 |
| Artifact | 显式`read_artifact`；广告方法缺失时本地失败 | 仅Server广告能力时可用 | 自动分页与摘要汇总 |
| 公共包导出 | `harnessix.sdk`只导出Agent Client/Transport/Error；根包无公共导出 | 当前产品统一为Agent Protocol | 继续以治理测试防止第二套SDK回流 |

## 5. 模块上下文、依赖方向与信任边界

```mermaid
flowchart LR
    CLI["Thin Agent CLI"] --> Agent["AgentClient"]
    Host["Python Host"] --> Agent
    Agent --> Port["AgentTransport"]
    Port --> InProc["InProcessAgentTransport"]
    Port --> Sub["SubprocessAgentTransport"]
    InProc --> Server["AgentProtocolServer"]
    Sub --> Stdio["App Server stdio"]
    Agent --> Protocol["Protocol Models"]
```

**图示说明：** Agent SDK以Protocol为唯一公共合同，可选择进程内或stdio边界。旧Action HTTP客户端源码已删除。

### 5.1 允许依赖

| 方向 | 规则 |
|---|---|
| CLI/宿主 → SDK | 允许；负责保存业务身份、呈现交互并决定重连 |
| AgentClient → Protocol | 允许；必须复用Params、Result和兼容校验，不复制Schema |
| SubprocessTransport → OS Process | 允许；仅argv，无Shell |
| InProcessTransport → App Server | 允许；只能调用Frame入口和Close，不旁路Service |
| 旧HTTP Client → Domain + httpx | 仅允许既有白名单调用方迁移，不允许新增生产依赖 |

### 5.2 禁止旁路

- SDK不得直接打开Session、Protocol Request或Effect Journal数据库；
- Agent Client不得把JSON-RPC序号用作Command `requestId`；
- Live Delta不得写入调用方持久游标；
- 旧HTTP Client不得重新进入公共导出或默认产品装配；
- 子进程stdout不得混入诊断文本；
- 自定义Transport不得用Notification的Response补偿Request；
- Client Instance ID不得作为远程认证身份；
- SDK不得把stderr、Prompt、Tool参数或HTTP错误正文默认写日志。

## 6. 包结构与推荐阅读顺序

| 顺序 | 文件 | 规模 | 阅读目标 |
|---:|---|---:|---|
| 1 | [`errors.py`](../../src/harnessix/sdk/errors.py) | 22行 | Agent SDK跨Transport共享的稳定错误合同 |
| 2 | [`response.py`](../../src/harnessix/sdk/response.py) | 119行 | 严格Response Envelope/JSON预算和Result错误归一 |
| 3 | [`request.py`](../../src/harnessix/sdk/request.py) | 82行 | 出站Frame、广告方法、协商消息/Replay上限和唯一Response关联 |
| 4 | [`subprocess.py`](../../src/harnessix/sdk/subprocess.py) | 349行 | 请求容量、Response路由、子进程标准流和取消安全关闭 |
| 5 | [`agent_client.py`](../../src/harnessix/sdk/agent_client.py) | 406行 | Transport端口、握手、Thread/Turn/Event客户端及兼容重导出 |
| 6 | [Protocol模块设计](protocol.md) | 现行设计 | 理解Params、Result、Cursor、兼容与错误合同 |
| 7 | [App Server模块设计](app-server.md) | 现行设计 | 对照Server握手、乱序响应、关闭和恢复 |
| 8 | [`test_server_sdk.py`](../../tests/app_server/test_server_sdk.py) | 1226行 | Client、Transport、Server与Runtime纵向场景，含容量、取消关闭、恶意Response、半握手和协商门禁 |
| 9 | [`test_agent_cli.py`](../../tests/app_server/test_agent_cli.py) | 190行 | 验证SDK如何被薄交互层消费 |
| 10 | [`__init__.py`](../../src/harnessix/sdk/__init__.py)与[根包导出](../../src/harnessix/__init__.py) | 公共面 | 验证只公开Agent SDK |

## 7. Agent SDK内部架构

```mermaid
flowchart TB
    subgraph AgentBranch["Agent Protocol branch"]
        AC["AgentClient"] --> F["_frame and _send"]
        F --> AT["AgentTransport"]
        AT --> IP["InProcess"]
        AT --> SP["Subprocess"]
        SP --> Capacity["shared pending capacity"]
        SP --> Router["response router"]
        SP --> Child["child process owner"]
        Router --> Pending["pending and abandoned"]
        Child --> Reader["single Response Reader"]
        Child --> Err["stderr tail"]
    end
```

`agent_client.py`拥有协议Client与进程内Transport；`subprocess.py`拥有连接级容量、Response身份和子进程资源。
SDK包不存在HTTP资源Client，不得新增绕过Agent Protocol的公共Base Client。

## 8. 公共API与导出边界

### 8.1 `harnessix.sdk`导出

| 导出 | 类型 | 用途 |
|---|---|---|
| `AgentClient` | 异步类 | Agent Protocol资源与事件 |
| `AgentSDKError` | 异常 | Agent协议、服务和Transport稳定错误 |
| `AgentTransport` | Protocol | 自定义Agent传输端口 |
| `InProcessAgentTransport` | 类 | 嵌入Server和确定性测试 |
| `SubprocessAgentTransport` | 类 | 本地stdio子进程 |

### 8.2 根包导出边界

[`harnessix.__init__`](../../src/harnessix/__init__.py)不导出业务符号。Agent调用方必须从`harnessix.sdk`
导入Agent SDK；不存在可直接导入的旧HTTP Client。

## 9. AgentTransport合同

```python
class AgentTransport(Protocol):
    async def exchange(self, frame: bytes) -> tuple[bytes, ...]: ...
    async def notify(self, frame: bytes) -> None: ...
    async def close(self) -> None: ...
```

### 9.1 语义

| 方法 | 调用方预期 | 官方实现 |
|---|---|---|
| `exchange` | 发送一个Request，返回Response帧集合 | InProcess可返回Server元组；Subprocess固定返回单元素元组 |
| `notify` | 发送Notification且不等待Response | InProcess同步检查无Response；Subprocess只写stdin |
| `close` | 有界结束Transport拥有资源 | InProcess关闭Server；Subprocess关闭stdin并逐级等待/终止 |

`AgentClient._send`在Transport返回后再次要求恰好一个Response，因此自定义Transport返回0或多个帧会得到
`invalid_response`。Transport端口没有声明每次调用Timeout、并发上限、取消隔离、重连或所有权转移，
自定义实现需要自行与Client语义对齐。

## 10. InProcessAgentTransport

进程内Transport不绕过Protocol：`exchange`和`notify`都调用同一个
[`AgentProtocolServer.process_frame`](../../src/harnessix/app_server/server.py)。它不做序列化之外的快捷访问，
因此Params、连接状态、方法和公共投影仍会执行。

```mermaid
sequenceDiagram
    participant C as AgentClient
    participant T as InProcessTransport
    participant S as AgentProtocolServer
    C->>T: exchange JSONL bytes
    T->>S: process_frame
    S-->>T: tuple of response bytes
    T-->>C: same tuple
    C->>T: notify JSONL bytes
    T->>S: process_frame
    alt empty tuple
        T-->>C: return
    else any response
        T-->>C: AgentSDKError invalid_response
    end
```

### 10.1 与子进程不等价的边界

调用协程取消会直接取消`process_frame`协程；子进程Transport则用`asyncio.shield`保留服务端请求并把ID记入
`_abandoned`。因此进程内Transport适合协议集成测试和嵌入，但当前不能证明其在所有提交切点具有与跨进程
完全相同的取消语义。Closing状态的Server会对Notification返回错误帧，进程内`notify`会立即报错；
子进程实现通常在Reader中异步把连接标记为失败。

## 11. SubprocessAgentTransport生命周期

`SubprocessAgentTransport`公开状态由`SubprocessTransportSnapshot.state`表达，内部由Closed Flag、Close Task、
Router Failure和Child Process组合得出：

```mermaid
stateDiagram-v2
    [*] --> not_started: constructed
    not_started --> running: first exchange or notify
    not_started --> closing: close
    running --> failed: response or pipe failure
    running --> exited: child exits before router observes EOF
    running --> closing: close
    failed --> closing: close
    exited --> closing: close
    closing --> closed: owned close task settles
    closed --> [*]
```

| 快照状态 | 判定 | 行为 |
|---|---|---|
| `not_started` | 未关闭、无Router Failure、Child未创建 | 首次操作惰性启动 |
| `running` | Child存在且未退出 | 可并发Exchange；写入串行 |
| `exited` | Child Return Code已出现、Reader尚未固定Failure | 下一操作或Reader收敛为失败 |
| `failed` | Router已固定首个Failure | 新Request及容量等待者失败，不自动重启 |
| `closing` | Closed且唯一Close Task未完成 | 拒绝新操作；资源回收继续 |
| `closed` | Close Task已完成 | Close幂等；操作固定失败 |

Router Failure采用首次错误优先。进程退出后Child引用不清空，当前实例不存在重启路径。调用方必须创建新Transport
和Client，并复用持久业务身份完成恢复。

## 12. 子进程传输职责拆分与启动

0.9.3a把原`agent_client.py`中的子进程实现拆为四个单一职责对象：

| 组件 | 唯一职责 | 关键状态 |
|---|---|---|
| `_RequestCapacity` | 为Pending与Abandoned分配共享槽位，并在连接失效时唤醒等待者 | `BoundedSemaphore`、Unavailable Event |
| `_ResponseRouter` | 管理Request ID、Future、迟到墓碑、严格Response解析和首个Failure | Pending Map、Abandoned Set、Capacity、Failure |
| `_ChildProcess` | 创建直接子进程、串行stdin、排空stdout/stderr并执行关闭升级 | Process、Start/Write Lock、Reader/Stderr Task、Tail |
| `SubprocessAgentTransport` | 校验配置、组合前三者、定义Exchange/Notify/Close和低敏快照 | Closed、Close Lock、Close Task |

`_ChildProcess.start`由`start_lock`保护，只调用一次`asyncio.create_subprocess_exec`。命令保存为不可变Tuple，
不经过Shell；当前不设置`cwd`、`env`、`start_new_session`、Windows Creation Flags或文件描述符白名单，因此子进程
继承父进程工作目录和默认环境。

构造参数经`ProtocolLimits`校验：`max_message_bytes`允许4 KiB～8 MiB，`max_pending_requests`使用协议合同范围；
两个关闭Timeout必须大于零。启动成功后创建：

- `harnessix-sdk-reader`：按`max_message_bytes + 1`读取stdout并交给Router严格归并；
- `harnessix-sdk-stderr`：每次读取4096字节，内部只保留最近65,536字节；
- PIPE stdin：所有写入通过`write_lock`；
- PIPE stdout/stderr：分别由唯一Task读取，避免子进程因缓冲区填满阻塞。

启动`OSError`映射为`server_start_failed`且不公开系统异常。当前没有启动Deadline，也不验证可执行文件来源、签名或目录权限。

## 13. 共享未决容量与Response归并

```mermaid
sequenceDiagram
    participant A as Caller A
    participant B as Caller B
    participant T as SubprocessTransport
    participant C as RequestCapacity
    participant P as App Server
    participant R as ResponseRouter
    A->>C: reserve slot
    C-->>A: slot
    A->>R: register id 1
    A->>P: write id 1 under write lock
    B->>C: reserve slot
    C-->>B: slot or backpressure
    B->>R: register id 2
    B->>P: write id 2 under write lock
    P-->>R: response id 2
    R->>C: release slot 2
    R-->>B: settle future 2
    P-->>R: response id 1
    R->>C: release slot 1
    R-->>A: settle future 1
```

### 13.1 容量不变量

```text
len(pending) + len(abandoned) <= max_pending_requests
```

`exchange`在注册Future和写入前取得一个槽位。重复Request ID在注册处失败并立即归还槽位；正常Response、写前/写中
非取消失败、迟到Response和连接级Fail各自只释放一次。连接关闭或Reader失败会设置Unavailable Event，使尚在等待容量的
调用立即检查并获得同一稳定错误，而不是永久挂起。

构造期`max_pending_requests`约束SDK本地内存身份；Server在握手后另行执行协商Pending上限。当前AgentClient不会在
Initialize Result返回后动态改写Transport容量，产品组合应让客户端构造上限不高于预期服务端上限。即使调用方配置更大，
Server仍会按协商值背压，不会因此放开服务端并发。

### 13.2 写侧

1. `_frame_id`先解析Request ID，拒绝无效JSON、非对象、布尔或缺失ID；
2. Child惰性启动；
3. Router保留一个共享容量槽位并注册Future；
4. 活跃或Abandoned ID重复时返回`duplicate_request_id`；
5. `write_lock`串行执行`stdin.write + drain`；
6. `asyncio.shield(future)`阻止调用方取消向Response Future传播。

检查和登记在单事件循环内无`await`，但Transport不承诺跨线程或跨事件循环使用。等待容量的调用Task数量本身由上层
并发决定；共享上限约束的是需要保留身份的Pending与Abandoned状态。

### 13.3 读侧

Router逐行调用`_decode_response`，校验UTF-8、单对象、重复键、非有限数、深度、集合预算、JSON-RPC版本、
Response联合类型和安全ID。普通Response从Pending取出并结算Future；Abandoned Response只删除墓碑、释放槽位并丢弃正文。
无效Envelope、未知ID、超限帧、EOF或读取错误会固定首个Failure、失败全部Pending、清空Abandoned、释放所有槽位并唤醒
容量等待者。

这种Fail-closed策略避免错配Response：一条未知或畸形帧会终止整条连接上的所有并发调用。恢复必须按业务Command ID、
Thread Snapshot和Replay逐项核对，不能盲目重提。

## 14. 取消与迟到Response

```mermaid
sequenceDiagram
    participant C as Caller
    participant T as Transport
    participant R as ResponseRouter
    participant S as Server
    C->>T: exchange id N
    T->>R: reserve and register N
    T->>S: request N
    C-xT: cancel awaiting coroutine
    T->>R: pending N becomes abandoned N
    Note over R: slot remains occupied
    S-->>R: late response N
    R->>R: discard body and tombstone
    R->>R: release slot
```

子进程Request写出后，调用取消不等于Server业务取消。Pending身份转为Abandoned，容量继续占用，直到迟到Response到达
或连接关闭。这个选择防止未知Response击穿连接，也把高频取消的最坏内存固定在`max_pending_requests`；代价是Server永不
响应时新Request会持续背压，调用方应关闭连接并按持久身份恢复，而不是TTL淘汰墓碑。

非取消异常会从Pending移除身份并释放槽位。若stdin发生不可判定的部分写，后续迟到Response会成为未知ID并使连接失败；
这是保守失效，不是Exactly-once证明。

## 15. Notification只写路径

`notify`惰性启动Child并在`write_lock`内写入帧，不创建Pending Future、不占Request容量，也不等待Response。该路径用于
`notifications/initialized`。若Server违规回复Notification，Reader会把无效或未知ID固定为连接级`invalid_response`；
`notify`本次可能已经返回，后续操作观察Sticky Failure。进程内Transport则会在同一次`notify`立即发现非空Response，
两个Transport的错误时机仍不同。

## 16. stderr诊断与低敏资源快照

`_ChildProcess._drain_stderr`持续排空stderr，内部只保留最近64 KiB。Raw Tail不再作为Transport公共属性暴露；
`SubprocessAgentTransport.snapshot()`返回冻结`SubprocessTransportSnapshot`：

| 字段 | 语义 | 隐私边界 |
|---|---|---|
| `state` | 上述六态之一 | 低基数，不含进程身份 |
| `max_pending_requests` | 构造期共享容量 | 配置数字 |
| `pending_requests` | 当前活动Future数量 | 不含Request ID |
| `abandoned_requests` | 当前迟到墓碑数量 | 不含Request ID |
| `stderr_tail_bytes` | 内部Tail当前字节数 | 不返回正文 |
| `failure_code` | Router首个稳定错误Code | 不返回异常或Response正文 |

快照不含命令、PID、路径、环境、Request ID、Response或stderr正文，也不是持久恢复事实。Stderr Task自身异常仍可能在
Close Task结算时传播；正式诊断包还需要0.9.4统一脱敏和来源白名单。

## 17. 子进程关闭与强制终止

```mermaid
flowchart TD
    Close["public close"] --> Lock["close lock"]
    Lock --> Existing{"owned close task exists"}
    Existing -- no --> Mark["mark closed and fail router"]
    Mark --> Create["create one child shutdown task"]
    Existing -- yes --> Reuse["reuse same task"]
    Create --> Shield["await shield close task"]
    Reuse --> Shield
    Shield --> Stdin["close stdin under write lock"]
    Stdin --> Grace["wait configurable graceful deadline"]
    Grace -- timeout --> Term["terminate direct child"]
    Term --> TermWait["wait configurable terminate deadline"]
    TermWait -- timeout --> Kill["kill direct child and wait"]
    Grace -- exited --> Join["join reader and stderr"]
    TermWait -- exited --> Join
    Kill --> Join
    Join --> Done["close task settles"]
```

### 17.1 当前保证

- 第一次Close在Lock内设置Closed，立即Fail Router并唤醒Pending、Abandoned和容量等待者；
- Transport只创建一个Close Task，重复Close复用同一Task；
- 公共等待使用`asyncio.shield`：调用方取消会收到取消，但不能取消资源所有者Task；
- 后续Close可再次等待同一回收过程；
- stdin正常关闭给App Server一次EOF优雅退出机会；
- 优雅和Terminate期限均可配置且必须为正数，超时后逐级Terminate、Kill；
- Reader和stderr Task在Close Task返回前结算。

### 17.2 边界

- 只终止直接子进程，没有进程组/Job Object或任意后代进程树所有权；
- `process.kill()`后的最终Wait没有额外Timeout；
- stdin `drain`/`wait_closed`没有独立写入Timeout；
- stderr Task异常可能成为Close Task异常；
- 当前专项测试证明调用方取消后Close继续完成，但Terminate/Kill、Windows进程树和后代残留仍待三平台发布验证。

## 18. AgentClient状态与初始化

`AgentClient`持有Transport、Client身份、连接序号、初始化锁、失败标志和最近一次`InitializeResult`。它没有
完整连接状态枚举，也不自动创建替代Transport。

```mermaid
sequenceDiagram
    participant U as Caller
    participant C as AgentClient
    participant T as AgentTransport
    participant S as App Server
    U->>C: initialize
    C->>C: acquire initialize lock
    C->>S: initialize request with id 1
    S-->>C: InitializeResult
    C->>C: validate compatible result
    C->>T: notify initialized
    T->>S: notification
    C->>C: set initialized result
    C-->>U: same result object
```

### 18.1 初始化合同

| 字段 | 当前来源 | 真实行为 |
|---|---|---|
| `protocolVersion` | 固定`AGENT_PROTOCOL_VERSION` | 只发送1.0，无版本范围协商 |
| `clientInfo.name` | 构造参数，默认`harnessix-python-sdk` | 由Protocol模型校验 |
| `clientInfo.version` | 构造参数，默认`0.8.0` | 当前与包版本`0.1.0`不一致，不自动读取安装版本 |
| `clientInstanceId` | 注入或`uuid4()` | SDK不持久；调用方负责重连复用 |
| `capabilities.itemDeltas` | 硬编码`true` | 调用方不能关闭；其他能力使用模型默认值 |
| `limits` | `InitializeParams`默认 | 构造接口不允许调用方自定义 |

`_initialize_lock`使同一Client上的并发初始化只发送一次握手，后续调用返回同一个Result对象。SDK不会在其他
公开方法前自动初始化；未使用异步Context Manager或显式`initialize`时，Server会返回`not_initialized`。

### 18.2 部分握手失败

`initialized`只在Initialize Response验证成功且`notifications/initialized`写入返回后设置。Initialize任一步骤
失败或调用被取消时，Client设置`_initialize_failed`并关闭当前Transport；关闭错误不覆盖原始握手错误。后续
`initialize()`不再发送第二个Initialize，而是返回可重试`handshake_failed`。调用方必须创建新Transport和Client，
复用同一Client Instance ID完成完整握手；自动重建由0.9.1a的`RecoverableAgentSession`承担。

## 19. JSON-RPC序号与业务幂等身份

```mermaid
flowchart LR
    Seq["AgentClient sequence"] --> Rpc["JSON-RPC id"]
    Rpc --> Pending["connection response routing"]
    Caller["Caller request_id"] --> Command["Protocol command requestId"]
    Client["clientInstanceId"] --> Key["durable command key"]
    Command --> Key
    Key --> Ledger["Protocol Request Store"]
    Cursor["scannedThrough"] --> Replay["durable event resume"]
```

| 身份 | 生成者 | 生命周期 | 可否复用 |
|---|---|---|---|
| JSON-RPC ID | Client `_sequence += 1` | 当前Client/连接 | 不应在未决或Abandoned时复用 |
| Client Instance ID | 默认SDK或调用方 | 应跨Transport重建持久 | 同一逻辑客户端复用 |
| Command Request ID | 调用方 | 业务命令持久 | 相同意图重试复用；新意图必须新ID |
| Thread/Turn ID | Server/Runtime | Session持久 | 查询和恢复复用 |
| Replay Cursor | Server返回、调用方保存 | Thread事件历史 | 从最后`scannedThrough`继续 |
| Action ID | ActionRequest/Service | Action Journal持久 | HTTP查询和恢复复用 |

SDK有意要求所有写命令显式传入`request_id`，防止网络重试自动生成新业务身份。JSON-RPC ID只是当前
Response Future的路由键，即使相同Command跨重连，其JSON-RPC ID也可以不同。

## 20. Agent Response解析与错误映射

```mermaid
flowchart TD
    Bytes["response bytes"] --> Budget["UTF-8、Frame和JSON预算"]
    Budget -- Fail --> Invalid["AgentSDKError invalid_response"]
    Budget --> Envelope["strict Success or Error Response"]
    Envelope -- Fail --> Invalid
    Envelope --> Identity{"typed id equals local id"}
    Identity -- No --> Invalid
    Identity -- Yes --> Error{"JsonRpcErrorResponse"}
    Error -- Yes --> Map["AgentSDKError from typed error"]
    Error -- No --> Value["JsonRpcSuccessResponse.result"]
    Value --> Model["compatible target Result validation"]
    Model -- Fail --> InvalidResult["AgentSDKError invalid_result + path"]
```

### 20.1 已实现映射

- 非UTF-8、非单对象、重复键、非有限数、深度/集合/帧超限返回`invalid_response`；
- Envelope严格解析为`JsonRpcSuccessResponse`或`JsonRpcErrorResponse`，拒绝错误版本、额外字段、布尔ID和
  `result/error`共存或同时缺失；
- `code/message/retryable/path`由严格Error合同校验，不使用松散默认值；
- 公开方法通过`_validate_result`复用`validate_server_output`，继续忽略Result中新加可选字段；
- 已知Result字段非法时统一返回`invalid_result`，首个Pydantic错误位置进入有界`path`；
- Server返回的业务错误转换为`AgentSDKError(code,message,retryable,path)`。

### 20.2 兼容与剩余边界

Response Envelope属于JSON-RPC固定结构，因此拒绝未知顶层字段；Result内部继续按Protocol向前兼容规则忽略新增
可选字段，已知字段仍严格。自定义Transport返回的帧也经过相同解析。Initialize Response使用默认1 MiB校验；握手完成后`AgentClient`按服务端协商的更小`maxMessageBytes`校验后续
Request与Response，并在写Transport前拒绝未广告方法。`SubprocessAgentTransport`的StreamReader仍按构造期上限
分配，不能在握手后缩小底层缓冲；Pending并发与出站消息数量也尚未按协商值建立客户端容量门禁。

## 21. AgentSDKError合同

| 字段 | 来源 | 语义 |
|---|---|---|
| `code` | 本地Transport/解析或Server `error.data.code` | 稳定机器分类 |
| `message` | 本地固定中文或Server Error消息 | 人类诊断；当前未统一脱敏 |
| `retryable` | Server严格true或本地默认false | 仅提示，不自动重试 |
| `path` | Server字段路径的宽松过滤结果 | 追加到异常字符串，如`[field/0]` |

本地常见码包括`invalid_request`、`invalid_response`、`duplicate_request_id`、`server_start_failed`和
`server_closed`。Service/Kernel稳定码由Server透传。SDK不附加HTTP状态、子进程退出码、stderr摘要、
Request ID或Thread ID，也没有异常Cause链用于安全诊断。

## 22. Agent资源方法

### 22.1 Thread与Turn

| Client方法 | Protocol方法 | 输入重点 | 输出 |
|---|---|---|---|
| `create_thread` | `thread/create` | Workspace、Command Request ID | `ThreadView` |
| `get_thread` | `thread/get` | Thread ID | `ThreadView` |
| `list_threads` | `thread/list` | Cursor、Limit、Archived | `ThreadListResult` |
| `resume_thread` | `thread/resume` | Thread ID，无Command Request ID | `ThreadView` |
| `fork_thread` | `thread/fork` | Source、可选Through Turn、Request ID | `ThreadView` |
| `archive_thread` | `thread/archive` | Thread、Request ID、Reason | `ThreadView` |
| `start_turn` | `turn/start` | Thread、Prompt、Request ID、可选Budget | `TurnView` |
| `retry_turn` | `turn/retry` | Thread、Source Turn、Request ID、Budget | `TurnView` |
| `resume_turn` | `turn/resume` | Thread、Turn、Request ID | `TurnView` |
| `cancel_turn` | `turn/cancel` | Thread、Turn、Request ID | `TurnView` |
| `steer_turn` | `turn/steer` | Thread、Turn、Text、Request ID | `TurnView` |

### 22.2 交互、事件与Artifact

| Client方法 | Protocol方法 | 语义 |
|---|---|---|
| `respond_approval` | `approval/respond` | 调用方直接提供完整严格Params，绑定Approval指纹和决定 |
| `respond_question` | `question/respond` | 调用方提供Question身份和答案 |
| `replay_events` | `events/replay` | 单次持久事件页 |
| `next_events` | `events/next` | 持久进展优先，附带可选Live Delta与Timeout |
| `read_artifact` | `artifact/read` | Thread授权下分页读取；调用前统一检查广告方法 |
| `watch_thread` | 循环`events/next` | 无限异步迭代，不自动判断终态 |

所有方法先由本地Pydantic Params模型校验。该校验失败直接抛Pydantic异常，不转换为`AgentSDKError`；调用方
若需要统一错误呈现必须当前同时处理本地合同异常和SDK错误。

## 23. 能力协商与调用门禁

Initialize Result被保存到`client.initialized`，包含Methods、Artifact、Replay、Delta和Limits。握手完成后：

- `_send`在构造Frame后、调用Transport前检查方法是否位于`capabilities.methods`；
- 未广告方法返回`method_not_negotiated`，不会占用Transport写入；
- Frame超过协商`maxMessageBytes`时返回`negotiated_limit_exceeded`；
- Response也按协商`maxMessageBytes`再次验证；
- Replay/Next的请求`limit`超过`maxReplayEvents`时在写入前失败；
- `itemDeltas`仍以Server Result为准，Product UI投影只把实际收到的Delta作为临时显示。

Subprocess Transport已经以构造期`max_pending_requests`限制`Pending + Abandoned`，但当前不会在握手后按
Initialize Result动态缩小该容量；Server仍执行协商后的真实Pending/Outbox上限。SDK没有Notification出站队列，
也不根据布尔`replay`或`artifactPages`单独判断；对应方法是否存在以`capabilities.methods`为最终调用门禁。

## 24. Replay、Delta与watch_thread

```mermaid
flowchart TD
    Start["watch thread with after cursor"] --> Next["events next"]
    Next --> Page["EventsNextResult"]
    Page --> Advance["cursor equals max current and scannedThrough"]
    Advance --> Yield["yield page to caller"]
    Yield --> Next
    Page -. "Delta does not advance cursor" .-> Yield
```

`watch_thread`每轮调用`next_events`，只使用`page.replay.scanned_through`推进局部Cursor，然后无条件Yield页面。
这保留以下服务端语义：

- 被隐藏内部事件造成Cursor跳跃时不会反复扫描；
- Live Delta不改变持久恢复点；
- `timedOut=true`页面也会交给调用方；
- `liveGap`由调用方决定停止拼接并等待持久Item；
- `liveHasMore`不会触发SDK内部批量排空策略。

迭代器没有终止条件、退避、Cursor持久化、自动重连或Turn筛选。调用方Break只停止下一次循环，不关闭Client；
在Yield点没有隐藏的进行中Request。薄CLI在SDK之上读取Thread终态并处理Question、Approval和Artifact。
薄CLI的Replay与Next请求页大小取`min(256, initialized.limits.maxReplayEvents)`；因此小于默认值的服务端协商
结果也会在调用SDK前生效，不会依赖服务端二次拒绝。

## 25. 断线恢复责任

```mermaid
sequenceDiagram
    participant App as Host Application
    participant Old as Old AgentClient
    participant New as New AgentClient
    participant Server as New App Server
    App->>Old: request with durable command id
    Old--xApp: transport error or cancellation
    App->>App: keep client instance id and command id and cursor
    App->>New: construct new transport with same client instance id
    App->>New: initialize
    New->>Server: replay same command if result uncertain
    Server-->>New: same durable result or conflict
    App->>New: replay events after stored cursor
    New-->>App: authoritative progress
```

SDK不会自动执行图中恢复动作。宿主必须持久或可靠保存：

1. 稳定Client Instance ID；
2. 每个写命令的Request ID及其业务意图；
3. Thread/Turn ID；
4. 每个Thread最后已消费的`scannedThrough`；
5. 旧Action数据库不经SDK迁移，按离线归档手册保存。

若宿主使用默认随机Client Instance ID并在崩溃后重新构造Client，相同Command Request ID进入新的幂等命名
空间，不能依赖Protocol Request Ledger返回旧结果。领域层部分操作仍有确定性身份，但SDK合同要求显式复用。

## 26. 已删除Action Plane HTTP客户端（历史）

本节冻结记录Revision `3f37fe8`之前的兼容实现，不代表当前产品接口，所述源码和测试均已删除。
`HarnessixClient`和`HarnessixAsyncClient`曾是手写的轻量HTTP包装，分别拥有`httpx.Client`和
`httpx.AsyncClient`。构造参数只开放Base URL、Timeout和可选Transport；Base URL会移除尾部斜杠，默认
`http://127.0.0.1:8787`。异步Context Enter不会发健康检查或初始化请求，只返回自身。

```mermaid
sequenceDiagram
    participant F as Framework or App
    participant C as Harnessix HTTP Client
    participant H as httpx
    participant A as Action API
    F->>C: submit ActionRequest
    C->>H: POST v1 actions JSON
    H->>A: HTTP request
    A-->>H: 200 or 202 ActionSnapshot
    H-->>C: Response
    C->>C: raise for error then model validate
    C-->>F: ActionSnapshot
```

HTTP Client不拥有Action执行生命周期。Inline模式可能直接返回终态；Queued或等待状态可能返回202和非终态
Snapshot，调用方需要用`get`继续查询、处理审批或触发Reconcile。当前
[Adapter模块](adapters.md)只调用Submit并把完整Snapshot序列化为Tool内容，不会自动调用这些恢复方法。

## 27. HTTP资源方法与线路

| 同步/异步方法 | HTTP | 请求 | 成功输出 | 当前分页 |
|---|---|---|---|---|
| `submit` | `POST /v1/actions` | `ActionRequest`去除None后的JSON | `ActionSnapshot` | 不适用 |
| `get` | `GET /v1/actions/{action_id}` | Path ID | `ActionSnapshot` | 不适用 |
| `decide_approval` | `POST /v1/actions/{id}/approval` | `ApprovalDecision` JSON | `ActionSnapshot` | 不适用 |
| `reconcile` | `POST /v1/actions/{id}/reconcile` | 无Body | `ActionSnapshot` | 不适用 |
| `events` | `GET /v1/actions/{id}/events` | Path ID | `list[ActionEvent]` | 无，读取完整列表 |
| `tools` | `GET /v1/tools` | 无 | `list[ToolDescriptor]` | 无，读取完整列表 |

Client没有封装`/healthz`、`/readyz`、OpenAPI或服务版本。`action_id`类型允许UUID或任意字符串，SDK不先
执行UUID规范化；服务端路由负责最终校验。同步与异步方法当前手工重复，变更任一端点时必须成对维护。

## 28. HTTP成功与错误解析

```mermaid
flowchart TD
    Resp["httpx Response"] --> Success{"is success 2xx"}
    Success -- Yes --> Kind{"snapshot events or tools"}
    Kind --> Validate["Pydantic model_validate"]
    Success -- No --> Structured{"body has error code and message"}
    Structured -- Yes --> APIErr["HarnessixAPIError status code message"]
    Structured -- No --> Fallback["http_error plus full response text or reason"]
    Fallback --> APIErr
```

### 28.1 成功

`_snapshot`先调用`_raise_for_error`，再把完整JSON解析为`ActionSnapshot`。Events和Tools方法先检查HTTP状态，
再读取顶层`events/tools`数组并逐项验证。HTTP 202属于成功，因此不会抛错；Snapshot状态保留服务端事实。

### 28.2 非成功

若响应满足`{"error":{"code":...,"message":...}}`，SDK保存HTTP状态、字符串化Code和Message；否则使用
`code=http_error`以及完整`response.text`或Reason Phrase。当前：

- 不解析`retryable`、字段Path、Trace ID、Retry-After或错误Schema版本；
- Fallback正文无长度上限和脱敏，受控探针可形成10万字符Error Message；
- 结构化Error的Code/Message也没有客户端长度复验；
- 网络、TLS、Timeout和连接池异常直接保留为`httpx`异常；
- 成功Body缺失/损坏时直接抛JSON、KeyError或Pydantic异常；
- 不自动重试任何方法，也不区分幂等GET与可能提交副作用的POST。

## 29. HarnessixAPIError合同

| 字段 | 语义 | 限制 |
|---|---|---|
| `status_code` | HTTP状态 | 网络失败没有该异常类型 |
| `code` | 服务端错误码或`http_error` | 任意值转字符串，无长度检查 |
| `message` | 服务端Message或Fallback Body | 可能很大，未脱敏 |
| 异常字符串 | `code: message` | 不含Action ID、Trace ID或Retry提示 |

该异常与`AgentSDKError`没有共同基类，调用方不能用一个SDK异常捕获两套协议。对于Action提交响应丢失，
调用方应使用原Action ID或Idempotency Key查询，不应因只看到`httpx`异常就生成全新身份盲目重试。

## 30. 持久状态、内存状态与所有权

| 状态/资源 | 所有者 | 是否持久 | 释放/恢复 |
|---|---|---:|---|
| Agent Client Instance ID | AgentClient/调用方 | SDK不持久 | 调用方保存并注入新Client |
| JSON-RPC Sequence | AgentClient | 否 | 新Client从0开始，不影响业务幂等 |
| Initialize Result | AgentClient | 否 | 新连接重新握手 |
| Subprocess Handle | `_ChildProcess` | 否 | Transport Close或进程退出 |
| Pending Future | `_ResponseRouter` | 否 | Response、非取消失败、Reader失败或Close |
| Abandoned ID | `_ResponseRouter` | 否 | 迟到Response或Close；与Pending共享容量 |
| Reader Error | `_ResponseRouter` | 否 | 首个Failure Sticky到实例结束 |
| Request Capacity | `_RequestCapacity` | 否 | 正常/迟到Response、Request失败或Router Fail释放 |
| Close Task | `SubprocessAgentTransport` | 否 | 唯一且可重复等待；调用方取消不取消它 |
| stderr Tail | `_ChildProcess` | 否 | 内部最多64 KiB；公共快照只返回字节数 |
| Replay Cursor | `watch_thread`局部/调用方 | SDK不持久 | 调用方保存`scannedThrough` |
| HTTP连接池 | HTTP Client | 否 | Context Exit或Close |
| Action/Session事实 | Server Store | 是 | SDK只按ID查询，不拥有 |

SDK Close不删除Server数据库、Thread、Action或Artifact。AgentClient Close会关闭其Transport；InProcess
Transport因此也关闭注入的Server/Service，说明该适配器把Server生命周期所有权转交给Client。HTTP Client
只关闭自身连接池，不关闭远程Action Service。

## 31. 并发、线性化点与资源预算

### 31.1 Agent分支

| 操作 | 线性化/排序点 | 当前边界 |
|---|---|---|
| JSON-RPC ID分配 | `_sequence += 1`后构造Request | 同事件循环内无Await；非线程安全 |
| 进程创建 | `_start_lock`内设置`_process` | 仅一次，不自动重启 |
| 容量保留 | `_RequestCapacity.reserve` | Pending与Abandoned共享构造期上限；失败时唤醒等待者 |
| Pending注册 | `_ResponseRouter.register(id)` | 取得槽位后、写帧前完成 |
| 帧写入 | `_write_lock`内`write + drain` | 无SDK级写Timeout |
| Response结算 | Router `pop(pending[id])` | 可乱序；释放槽位；未知ID全局失败 |
| 取消登记 | Router把Pending转为Abandoned | 槽位不释放，迟到Response或Close释放 |
| 关闭开始 | `_closed = true`并创建唯一Close Task | 拒绝后续Start/Write，Fail Router并唤醒容量等待者 |

客户端已有构造期Response字节与Reader行长上限，且`Pending + Abandoned`最多为`max_pending_requests`；
超量调用在注册Future前等待容量。Server协商Limit不会动态调整该结构，因此产品构造值和Server上限仍需保持一致。

### 31.2 HTTP分支

并发、连接池、DNS和每阶段Timeout由`httpx`默认与传入总Timeout控制。SDK不暴露Limits、连接池配置、
重试或并发Semaphore，也没有Events/Tools页大小。同步Client不得从异步事件循环阻塞调用，异步Client由
调用方负责在单一生命周期内复用。

## 32. 失败与恢复矩阵

| 场景 | 当前结果 | Server事实 | 恢复动作 |
|---|---|---|---|
| 子进程启动失败 | `server_start_failed` | 未启动 | 修正命令后新建Transport |
| Request本地JSON/ID无效 | `invalid_request` | 未写入 | 修正自定义Frame |
| Pending ID重复 | `duplicate_request_id` | 原请求可能在执行 | 等待原请求；不要换业务身份盲重试 |
| stdin写入失败 | `server_closed` | 未知是否部分写入 | 新连接，以原Command ID查询/重放 |
| stdout非法/超限Envelope | 全部Pending `invalid_response`，Reader Sticky | 各请求未知 | 关闭进程；按业务ID逐项恢复 |
| stdout EOF | 全部Pending `server_closed` | Session可能已提交 | 新建Client，Snapshot+Replay |
| 未知Response ID | 整连接失败 | 其他Request可能正常 | 关闭并按每个业务身份恢复 |
| 调用取消 | Subprocess记Abandoned且保留容量；InProcess向Server协程传播取消 | 取决于Transport与提交切点 | 查询Thread/Action，不把取消当作回滚；必要时关闭连接释放墓碑 |
| 等待Request容量时连接关闭 | 等待者收到`server_closed` | 尚未写入 | 新建Transport后按原业务意图重试 |
| Close等待被调用方取消 | 调用方收到取消，Owned Close Task继续 | 已提交事实不变 | 后续再次Close等待同一Task |
| Initialize Response后Notify失败 | 原错误；当前Client后续固定`handshake_failed` | 无领域命令 | Transport已关闭；新建Client并复用同Client ID |
| Server返回Error | `AgentSDKError` | 取决于错误 | 依据Code/Retryable及Snapshot |
| Result结构损坏 | `invalid_result`及首个安全字段路径 | 未知 | 不盲重提；关闭不可信连接并保留业务身份 |
| `watch_thread`超时页 | 正常Yield | 无新增事实 | 调用方退避或继续 |
| Live Gap | 正常Yield `liveGap` | 持久Item仍权威 | 放弃Delta拼接，等Replay终态 |
| SDK崩溃 | 全部内存状态丢失 | Server持久事实保留 | 从外部保存ID/Cursor重建 |
| HTTP 202 | 返回非终态Snapshot | Action已受理 | `get`轮询或等待外部Worker |
| HTTP非2xx结构化错误 | `HarnessixAPIError` | 按状态码/Code | 使用稳定Action身份判断 |
| HTTP响应丢失/Timeout | 原始`httpx`异常 | 提交是否成功未知 | 用原Action ID/Idempotency Key查询 |
| HTTP成功Body损坏 | JSON/Key/Pydantic异常 | 服务端可能已成功 | 不盲重提；按Action ID查询并诊断版本 |

## 33. 安全与隐私边界

```mermaid
flowchart LR
    User["Prompt and decisions"] --> Agent["AgentClient memory"]
    Agent --> Stdin["child stdin JSONL"]
    Child["App Server diagnostics"] --> Stderr["raw 64 KiB tail"]
    Action["ActionRequest and SecretRef"] --> HTTP["httpx request"]
    HTTP --> API["Action API"]
    Bad["untrusted response"] --> Error["SDK exception text"]
```

### 33.1 已有控制

- 子进程使用argv，不使用Shell；
- 启动OSError不公开原始系统异常；
- Agent领域字段先通过Protocol Params模型；
- 未预期Server异常正常应由App Server清洗；
- HTTP请求使用Domain模型序列化，Secret应以`SecretRef`表达；
- SDK本身没有默认Logger，不主动把请求/响应写盘；
- stderr只保留固定64 KiB，避免无限累积。

### 33.2 信任假设与缺口

- 子进程继承父进程默认环境和当前目录；任意自定义Command可看到父进程凭据；
- 不验证App Server可执行文件签名、Owner或路径；
- stdout Response有严格Envelope、字节、深度和集合预算，但不认证对端进程身份；
- 内部stderr Tail、Server稳定Message和历史HTTP Fallback Body未脱敏；公共Snapshot不返回Tail正文；
- 默认HTTP是明文Loopback，不包含身份认证；自定义远程Base URL没有强制HTTPS；
- Client Instance ID只是幂等命名空间，不是认证凭据；
- SDK不提供多租户Thread/Action授权；
- 调用方可能把异常字符串直接写日志，带出Server或HTTP正文。

任何远程Agent Protocol或Action API产品化都必须在SDK之外先建立认证、授权、TLS、凭据生命周期、租户隔离
和速率限制，再为Client增加显式安全配置；不能把本地默认值当作公网安全基线。

## 34. 平台、安装与部署

AgentClient、InProcess和HTTP包装没有显式OS分支。`asyncio.create_subprocess_exec`、PIPE和`httpx`可在
Python支持的平台运行，但当前不能据此宣称产品级三平台完成：

| 平台方面 | 当前事实 | 缺口 |
|---|---|---|
| macOS/Linux | 开发与CI覆盖大量Python路径 | 无安装器、升级和长连接发布证据 |
| Windows | SDK源码无主动拒绝，默认产品已有原生只读Runtime | Subprocess Transport取消安全关闭尚无Windows专项发布证据 |
| Python | 要求3.12+ | 仅Python SDK，无跨语言客户端 |
| Package Version | `pyproject`为0.1.0 | Agent Client默认报告0.8.0，身份元数据漂移 |
| Agent可执行入口 | 薄CLI传入Server Program与参数 | SDK不自动解析项目配置或验证可执行文件 |
| HTTP地址 | 默认Loopback 8787 | SDK不做Health/Ready探测或服务发现 |

三平台正式结论需要真实子进程启动、并发、取消、EOF、Terminate/Kill、Unicode argv/path和安装产物测试，
并与0.9.1 Windows产品入口及0.9.5发行验收共同关闭。

## 35. 可观测性与诊断

SDK当前没有注入Observability端口，不创建Span、Metric或结构化日志。可见诊断只有：

- `AgentSDKError`的Code、Message、Retryable和Path；
- `HarnessixAPIError`的HTTP Status、Code和Message；
- `SubprocessAgentTransport.snapshot()`的低敏状态、容量计数、stderr字节数和稳定失败Code；
- 调用方可访问的Initialize Result、Thread/Turn/Event和Action Snapshot；
- 原始`httpx`传输异常。

### 35.1 当前快照与建议Telemetry

`snapshot()`已经提供瞬时低敏Gauge来源，但不主动发布Metric，也不持久化。下列信号仍需由后续Observability端口
从快照和生命周期事件生成：

| 类型 | 信号 | 安全属性 |
|---|---|---|
| Counter | `sdk.agent.requests` | method、result class，不含ID |
| Histogram | `sdk.agent.request.duration` | method、transport kind |
| Gauge | `sdk.agent.pending` | transport kind |
| Gauge | `sdk.agent.abandoned` | transport kind |
| Counter | `sdk.agent.connection.failure` | start/read/write/eof/protocol/close |
| Histogram | `sdk.agent.stderr.bytes` | 只记录长度，不记录正文 |
| Counter | `sdk.http.requests` | method template、status class |
| Histogram | `sdk.http.duration` | route template，不放Action ID |
| Counter | `sdk.recovery.attempts` | command/replay，不放Thread ID |

Trace或日志不得记录Prompt、Question Answer、Approval Reason、Tool参数/结果、Workspace、Artifact正文、
Action Arguments、SecretRef解析值、stderr正文或HTTP Fallback Body。观测失败不能改变SDK请求结果。

## 36. 重点类、函数与接口

### 36.1 Agent分支

| 符号 | 职责 | 关键状态 | 失败/取消 |
|---|---|---|---|
| `AgentSDKError` | Agent客户端稳定错误 | code/message/retryable/path | 不自动重试 |
| `AgentTransport` | Request/Notification/Close端口 | 实现定义 | 端口未固定Timeout和取消隔离 |
| `InProcessAgentTransport` | 复用完整Server Frame入口 | Server引用 | 取消直达Server协程；Notification Response立即失败 |
| `_RequestCapacity` | Pending与Abandoned共享容量 | Bounded Semaphore、Unavailable Event | Router失败时唤醒等待者；成对释放 |
| `_ResponseRouter` | Response身份、Future、迟到墓碑和首个失败 | Pending、Abandoned、Failure、Capacity | 严格解析；未知ID/EOF使全连接失败 |
| `_ChildProcess` | 子进程与标准流所有权 | Process、Locks、Reader/Stderr Task、内部Tail | 启动/写入错误清洗；Close逐级终止 |
| `SubprocessAgentTransport` | 组合容量、子进程、并发和关闭 | Router、Child、Closed、Owned Close Task | Sticky失败；取消安全Close；低敏Snapshot |
| `response._decode_response` | 严格解析服务端Envelope | Frame/JSON预算 | 任一非法结构映射`invalid_response` |
| `response._validate_result` | 兼容读取具体Result | 首个错误路径 | 映射`invalid_result` |
| `_ChildProcess.start` | 惰性且唯一地创建子进程和Reader | `start_lock` | OSError映射启动失败 |
| `_frame_id` | 写前提取ID | 无 | 只做浅层JSON/ID检查 |
| `_ResponseRouter.fail` | 连接级失败广播 | Failure、Pending、Abandoned、Capacity | 首错误优先并唤醒容量等待者 |
| `_ResponseRouter.read` | 有界单Reader按严格ID结算 | Pending/Abandoned | 畸形/超限/未知/EOF使全连接失败 |
| `_ChildProcess._drain_stderr` | 避免Pipe阻塞并保留内部尾部 | 64 KiB Bytearray | 无脱敏；公共快照不返回正文 |
| `exchange` | 登记、串行写、等待或取消 | Future Map | Cancel登记迟到ID |
| `notify` | 只写Notification | Write Lock | 不同步等待违规Response |
| `SubprocessAgentTransport.snapshot` | 低敏资源诊断 | 无新增状态 | 不返回ID、命令、PID、路径或stderr正文 |
| `SubprocessAgentTransport.close` | EOF、Wait、Terminate、Kill | Closed Flag、唯一Close Task | 两段可配置期限；Shield保证调用方取消不打断回收 |
| `_frame` | Protocol Model到JSONL | 无 | UTF-8、紧凑JSON、禁止NaN |
| `AgentClient` | 握手、方法、结果和事件 | Sequence、Initialize Lock/Result、Client ID | Transport/Server/模型错误 |
| `AgentClient._send` | Request总模板 | Sequence | 严格Envelope、ID和类型化Error |
| `AgentClient.initialize` | 两步握手 | Initialize Lock/Result/Failed | 任一步失败关闭并禁止复用连接 |
| `AgentClient.watch_thread` | Pull-Live无限迭代 | 局部Cursor | 调用取消停止；无自动恢复 |

### 36.2 HTTP分支

| 符号 | 职责 | 所有状态 | 失败 |
|---|---|---|---|
| `HarnessixAPIError` | 非2xx稳定错误 | status/code/message | Fallback可能大且敏感 |
| `HarnessixClient` | 同步Action资源包装 | `httpx.Client` | 网络和成功Body异常透传 |
| `HarnessixAsyncClient` | 异步Action资源包装 | `httpx.AsyncClient` | 同上 |
| `_snapshot` | 状态检查和Snapshot校验 | 无 | Pydantic/JSON异常未包装 |
| `_raise_for_error` | 非2xx结构化/回退映射 | 无 | 完整Body进入Message |

## 37. 重点字段与不变量

### 37.1 Subprocess字段

| 字段 | 类型 | 不变量/生命周期 |
|---|---|---|
| `_child.command` | `tuple[str,...]` | 非空；不经Shell；内容不再变 |
| `max_message_bytes` | `int` | 4 KiB～8 MiB；构造后固定；约束stdout Reader与Response复核 |
| `_child.process` | optional Process | 最多赋值一次，不重启、不清空 |
| `_child.start_lock` | Async Lock | 串行Create与Shutdown读取Child引用 |
| `_child.write_lock` | Async Lock | stdin帧完整顺序和Close stdin互斥 |
| `_close_lock` | Async Lock | 唯一Close Task创建与复用 |
| `max_pending_requests` | `int` | 由Protocol Limits校验；约束Pending与Abandoned总数 |
| `_router.pending` | ID→Future | ID唯一；与Abandoned共享容量 |
| `_router.abandoned` | ID Set | 取消后等待一次迟到Response；占用槽位直到Response或Close |
| `_router.failure` | optional Pair | 首次错误Sticky，后续不覆盖 |
| `_child.reader_task` | optional Task | Process创建时同步创建 |
| `_child.stderr_task` | optional Task | 持续排空stderr |
| `_child.stderr_tail` | Bytearray | 内部最多65,536字节；Snapshot只公开长度 |
| `_close_task` | optional Task | 第一次Close创建，后续复用；由Transport拥有 |
| `_closed` | bool | 一旦true不回退；在Close Task创建前设置 |

### 37.2 AgentClient字段

| 字段 | 语义 | 不变量/限制 |
|---|---|---|
| `transport` | Frame边界所有者 | Close由Client转发 |
| `client_instance_id` | 命令命名空间 | UUID；默认随机；SDK不持久 |
| `client_name` | 握手信息 | Protocol模型最终校验 |
| `client_version` | 握手信息 | 默认0.8.0，与包版本漂移 |
| `_sequence` | JSON-RPC ID源 | 每Send递增；非线程安全 |
| `_initialize_lock` | 握手串行 | 只保护Initialize，不保护Close竞态 |
| `_initialize_failed` | 半握手失败标志 | 一旦true不回退；后续Initialize不再写当前Transport |
| `initialized` | 最近Result或None | Notify成功后才赋值；Close后不清空 |

### 37.3 HTTP字段

同步/异步客户端都只拥有`_client`。Base URL、Timeout、Transport进入`httpx`对象后不另存，SDK没有公开
配置快照、运行时能力或关闭标志；调用方可通过内部字段观察，但这不是公共合同。

## 38. 核心业务逻辑伪代码

### 38.1 子进程Exchange

```text
exchange(frame):
    request_id := shallow_json_frame_id(frame)
    process := start_once()
    reserve one shared pending-or-abandoned slot
    future := new future
    require request_id not in pending or abandoned
    pending[request_id] := future

    try:
        under write_lock:
            require not closed, no reader_error, process alive
            write frame to stdin
            await stdin drain
        response := await shield(future)
        return one-element tuple(response)
    on caller cancellation:
        if pending still owns request_id:
            remove pending
            cancel local future
            add request_id to abandoned
            keep slot reserved until late response or close
        rethrow cancellation
    on other failure:
        remove and cancel local future if still pending
        release slot
        rethrow
```

### 38.2 Response Reader

```text
while line := stdout.readline():
    reject line over configured byte budget before unbounded buffering
    strictly parse JSON-RPC success-or-error response and safe id
    if id in abandoned:
        remove id, discard response and release slot
    else if id in pending:
        pop future, release slot and set raw response bytes
    else:
        fail every pending request, clear abandoned and release all slots
        store sticky reader error
        return

on EOF or read failure:
    fail every pending request, clear abandoned, release slots and wake capacity waiters
```

### 38.3 Agent Send

```text
sequence += 1
request := strict JsonRpcRequest(sequence, method, params)
responses := transport.exchange(frame(request))
require exactly one response
response := strict bounded JSON-RPC response parse
require typed response.id equals sequence
if response is typed error:
    use validated code, message, retryable and path
    raise AgentSDKError
return typed success result

public method:
    build strict Params
    raw := send(protocol method, params)
    validate target Result while ignoring added optional output fields
    map validation failure to invalid_result and safe field path
    return projected resource
```

### 38.4 HTTP Client

```text
call endpoint with Domain model JSON
if response is not 2xx:
    if body has error.code and error.message:
        raise HarnessixAPIError(status, code, message)
    raise HarnessixAPIError(status, http_error, full body or reason)
validate response JSON as ActionSnapshot, ActionEvent list or ToolDescriptor list
return value
```

## 39. 源码、测试与决策映射

### 39.1 Agent SDK源码与测试

| 设计元素 | 源码符号 | 测试 | 测试符号 |
|---|---|---|---|
| 并发初始化 | `AgentClient.initialize` | [`test_server_sdk.py`](../../tests/app_server/test_server_sdk.py) | `test_sdk_serializes_concurrent_initialize_calls` |
| Agent主链与命令重放 | `create_thread/start_turn/get_thread/replay_events` | 同上 | `test_agent_sdk_drives_turn_replay_and_duplicate_command` |
| 幂等冲突 | `AgentClient._send`错误映射 | 同上 | `test_same_command_key_with_different_prompt_returns_conflict` |
| 完成后未调度恢复 | 相同Client Instance与Command ID | 同上 | `test_completed_ledger_recovers_accepted_turn_after_restart` |
| Thread分页 | `list_threads` | 同上 | `test_thread_list_filters_before_pagination` |
| Service Close时Client读取终态 | `get_thread/close` | 同上 | `test_service_shutdown_is_bounded_and_persists_cancel` |
| Notification只写 | `SubprocessAgentTransport.notify` | 同上 | `test_subprocess_notification_does_not_wait_for_response` |
| Response乱序归并 | `_ResponseRouter.pending/read` | 同上 | `test_subprocess_transport_routes_out_of_order_responses` |
| Malformed Response全局失败 | `_ResponseRouter.fail/read` | 同上 | `test_subprocess_transport_fails_all_pending_on_malformed_response` |
| 严格Response Envelope | `response._decode_response/AgentClient._send` | 同上 | `test_sdk_rejects_invalid_response_envelopes`六类攻击输入、`test_sdk_rejects_response_over_json_depth_budget` |
| Response字节上限 | `SubprocessAgentTransport(max_message_bytes=...)` | 同上 | `test_subprocess_transport_rejects_oversized_response_frame` |
| Pending与Abandoned共享容量 | `_RequestCapacity`、`_ResponseRouter` | 同上 | `test_subprocess_transport_bounds_cancelled_and_pending_requests` |
| Close取消隔离 | `SubprocessAgentTransport.close`、`_ChildProcess.shutdown` | 同上 | `test_subprocess_transport_close_continues_after_caller_cancel` |
| Transport配置校验 | `SubprocessAgentTransport.__init__` | 同上 | `test_subprocess_transport_rejects_invalid_resource_limits` |
| Result错误归一 | `_validate_result` | 同上 | `test_sdk_normalizes_invalid_result_contract` |
| 半握手失败关闭 | `AgentClient.initialize/_initialize_failed` | 同上 | `test_initialize_notification_failure_makes_connection_unusable` |
| Question恢复 | `respond_question` | 同上 | `test_sdk_question_response_resumes_background_turn`、`test_completed_question_command_recovers_before_background_spawn` |
| Replay/Delta | `next_events/replay_events` | 同上 | `test_events_next_delivers_live_delta_then_durable_replay`、`test_events_next_marks_bounded_delta_buffer_gap` |
| Approval | `respond_approval` | 同上 | `test_sdk_approval_response_drives_decided_turn` |
| Artifact能力 | `read_artifact/initialize` | 同上 | `test_artifact_read_is_advertised_only_with_scoped_reader` |

### 39.2 SDK消费者

| 行为 | 测试 |
|---|---|
| CLI跨页列Thread且拒绝停滞Cursor | [`test_agent_cli.py`](../../tests/app_server/test_agent_cli.py) `test_thin_cli_lists_all_pages_and_rejects_stalled_cursor` |
| Question选择与Transcript保持 | 同文件`test_thin_cli_drives_question_and_preserves_model_transcript` |
| 快速完成后仍Replay最终文本 | 同文件`test_thin_cli_replays_final_text_when_turn_completed_before_follow` |
| 审批前分页读取Diff Artifact | 同文件`test_thin_cli_reads_diff_before_batch_approval` |
| 固定Workspace产品装配 | [`test_server_and_cli.py`](../../tests/product_config/test_server_and_cli.py) `test_fixed_workspace_rejects_other_thread_roots` |

### 39.3 已删除HTTP SDK与对端（历史证据）

| 行为 | 源码 | 测试 |
|---|---|---|
| Async Submit保留Action合同 | `HarnessixAsyncClient.submit` | [`test_sdk.py`](https://github.com/carrie1988/Harnessix/blob/3f37fe8ae0646d3327254ce9677110b94f7c5e80/tests/unit/test_sdk.py) `test_async_sdk_preserves_action_contract` |
| API正常/冲突/202/Readiness | [`api/app.py`](https://github.com/carrie1988/Harnessix/blob/3f37fe8ae0646d3327254ce9677110b94f7c5e80/src/harnessix/api/app.py) | [`test_api.py`](https://github.com/carrie1988/Harnessix/blob/3f37fe8ae0646d3327254ce9677110b94f7c5e80/tests/integration/test_api.py)四个服务端用例；不是Client专项覆盖 |
| LangChain Tool消费HTTP Client端口 | [`adapters/langgraph.py`](https://github.com/carrie1988/Harnessix/blob/3f37fe8ae0646d3327254ce9677110b94f7c5e80/src/harnessix/adapters/langgraph.py)及[Adapter模块设计](adapters.md) | [`test_langgraph_adapter.py`](https://github.com/carrie1988/Harnessix/blob/3f37fe8ae0646d3327254ce9677110b94f7c5e80/tests/unit/test_langgraph_adapter.py)只使用Fake Async Client，不证明真实HTTP或LangGraph ToolNode |

### 39.4 决策与研究

| 资料 | 与本模块关系 |
|---|---|
| [ADR 0001](../adr/0001-python-first-runtime.md) | Python 3.12、asyncio、Pydantic、FastAPI与生态集成基础 |
| [ADR 0070](../adr/0070-agent-protocol-v1-boundaries.md) | JSON-RPC ID、Command身份、公共投影与兼容 |
| [ADR 0071](../adr/0071-headless-app-server-and-sdk-lifecycle.md) | 双Transport、Notification只写、恢复责任和stdio生命周期 |
| [ADR 0072](../adr/0072-durable-interaction-and-pull-live-stream.md) | Next/Delta、Question、Approval和薄CLI消费 |
| [ADR 0081](../adr/0081-single-coding-agent-product-boundary.md) | 撤销旧HTTP客户端公共产品地位并约束迁移删除顺序 |
| [ADR 0089](../adr/0089-bounded-local-transport-lifecycle.md) | 共享迟到响应容量、取消安全Close与低敏Snapshot |
| [可靠性与性能研究](../research/reliability-and-performance.md) | Codex/OpenCode/Claude Code固定版本的背压和关闭证据 |
| [Agent Protocol与产品运行时研究](../research/agent-protocol-product-runtime.md) | Codex/OpenCode/Claude Code固定证据及Harnessix独立结论 |
| [0.8产品运行时设计](../m08-product-runtime-and-extensions.md) | App Server、SDK、交互与产品装配历史切片 |
| [Action Plane子系统设计](../subsystems/action-plane.md) | HTTP客户端对应的Action状态、错误和恢复事实 |

## 40. 测试设计与证据边界

### 40.1 已证明范围

- 同一AgentClient并发Initialize只执行一次并返回同一对象；
- 进程内Client完成Thread、Turn、幂等重放、冲突、恢复、Question、Approval和Artifact闭环；
- 子进程Notification不等待Response；
- 两个并发Request的逆序Response能按ID正确归并；
- 一个Malformed stdout帧会使全部Pending得到`invalid_response`；
- 布尔ID、错误版本、Envelope额外字段、Result/Error共存和重复字段均被拒绝；
- 超过构造预算的子进程Response在Reader边界失败，不交给业务Result解析；
- Pending与Abandoned共享容量，高频取消不会让身份集合超过构造上限；
- 取消后的迟到Response释放墓碑与容量，连接关闭会唤醒容量等待者；
- Close调用方被取消后，Transport拥有的Close Task继续完成，后续Close可复用同一Task；
- 非法消息、Pending和关闭Timeout参数均在启动进程前失败；
- 非法Result统一映射`invalid_result`并携带首个字段路径；
- Initialize Notification失败后当前Transport被关闭，第二次Initialize不再重复请求；
- 持久Replay、Live Delta、Gap和Deadline由纵向测试消费；
- 薄CLI仅依赖AgentClient实现协商上限内分页、交互、最终文本和Diff后审批；
- 历史HTTP SDK与API证据冻结在固定Git Revision，当前测试树不再运行该链；
- 产品收敛治理测试证明HTTP Client、API对端、依赖和生成规格没有回流。

### 40.2 尚未证明范围

- Initialize Response成功而Notification失败后的新Transport自动重建；
- Subprocess容量在Initialize后按协商值动态缩小；当前Server仍执行协商上限；
- 子进程启动Timeout、stdin写入Timeout和stderr Reader故障；
- Terminate/Kill真实升级、子进程后代清理和复合错误优先级；
- Windows原生、macOS/Linux安装产物和长时间真实Pipe测试；
- 调用取消后的Server业务自动取消；当前明确要求按持久身份恢复；
- 大规模并发、长会话、内存和泄漏Soak；
- SDK级Telemetry、Secret Redaction与诊断包。

## 41. 已知限制、风险与后续工作

| 优先级 | 限制/风险 | 影响 | 后续归属 |
|---|---|---|---|
| P2 | SDK本身不自动重建Transport | 直接SDK调用者需自行重连；Product UI的`RecoverableAgentSession`已经管理连接代际 | 保持分层，不下沉产品重试策略 |
| P2 | SDK不直接保存Client Instance、Command ID和Cursor | 直接调用者仍自行持久化；Product UI Store与Session已提供正式上层实现 | 保持SDK无状态边界 |
| P1 | 构造期Pending容量不会按握手协商值动态缩小 | Client可能比Server允许更多并发等待；Server仍会背压 | 0.9.3d并发Soak后决定是否增加可变门禁 |
| P1 | Close只终止直接进程，不拥有后代进程树 | 自定义App Server生成后代时可能残留 | 0.9.3c进程Owner、0.9.5平台发行 |
| P1 | stdin写入、`wait_closed`和Kill后的最终Wait没有独立Deadline | 异常平台Pipe可能拖延Owned Close Task | 0.9.3d真实Pipe故障注入 |
| P1 | 子进程继承默认环境和cwd，自定义Command不校验来源 | 第三方程序可读取父进程凭据和仓库上下文 | 0.9.4供应链与Secret边界 |
| P1 | 内部stderr Tail和Server Message无统一Redactor | 未来诊断消费若错误输出正文可能泄漏敏感信息 | 0.9.4错误清洗和诊断包 |
| P2 | 默认Agent Client Version为0.8.0而包版本为0.1.0 | 诊断身份不可信 | 从包元数据读取或统一版本源 |
| P2 | Notification违规Response在两个Transport的失败时机不同 | 嵌入与子进程错误呈现不一致 | Transport合同测试与统一状态 |
| P2 | 永不响应的Abandoned会占用容量直到Close | 不再无界增长，但连接可能停止接受新Request | 保守关闭连接并按持久身份恢复 |
| P2 | `watch_thread`无限Yield Timeout且不识别终态 | 消费者容易忘记退出或退避 | 提供独立高层Follow Helper，不改变底层流 |
| P2 | 根包不导出Agent SDK但包级导出 | 公共API发现与版本承诺不统一 | 发布API清单决策 |
| P2 | SDK无Telemetry；Snapshot仅按需读取 | 连接故障与恢复尚未形成产品SLO | 0.9.3d可观测性与Soak |

## 42. 验收标准

- [x] Agent SDK公共边界与旧HTTP客户端迁移边界已明确分离；
- [x] Agent Transport端口、InProcess与Subprocess差异对应实际源码；
- [x] 子进程启动、单Reader/Write Lock、Pending、Abandoned和Sticky失败完整；
- [x] 取消、迟到Response、Notification、stderr与关闭升级语义完整；
- [x] Agent握手、半握手失败、三类身份和能力Limit差距完整；
- [x] Thread、Turn、交互、Replay/Delta、Artifact与Watch方法完整；
- [x] 旧HTTP同步/异步资源、202、错误回退和Action恢复责任仅作为迁移资料保留；
- [x] 持久/内存事实、并发、资源、安全、平台和观测边界未被夸大；
- [x] 重点类、字段、伪代码、源码、测试、ADR和研究双向映射；
- [x] 链接、Mermaid、专项测试、全仓检查和文档索引同步均已通过。

## 43. 维护规则

以下变化必须在同一提交更新本文及对应Protocol/App Server文档；涉及已退役接口时同时更新0.9.1f收敛资料：

1. `harnessix.sdk`或根包公共导出变化；
2. `AgentTransport`方法、并发、取消或所有权合同变化；
3. 子进程Start、argv/env/cwd、Reader、Writer、stderr或Close顺序变化；
4. Pending、Abandoned、Reader Error、队列、Timeout或资源Limit变化；
5. Agent Response Envelope、错误映射和Result兼容读取变化；
6. 初始化字段、Capability、Limit、默认Client Version或半握手恢复变化；
7. Thread/Turn/Question/Approval/Event/Artifact方法签名和返回变化；
8. Cursor推进、Gap、Timeout、Watch终止或自动重连变化；
9. 旧HTTP客户端或第二套公共SDK重新出现；
10. Secret清洗、诊断、Telemetry、平台和发行边界变化。

重大SDK语义变化使用[重大变更设计模板](../governance/templates/change-design-template.md)评审。协议生成物更新不
自动证明手写Client兼容；必须同时执行恶意Response、取消、断线恢复和三平台子进程测试。

## 44. Workspace Patch客户端使用边界（0.9.1e3）

Python `AgentClient`不增加Patch专用传输API。客户端从事件流观察`patch_batch`审批，使用既有`read_artifact`按连续offset读取完整Review JSONL，核对记录数、UTF-8字节与SHA后，再使用`respond_approval`提交精确`request_fingerprint`。断线后通过Replay恢复同一审批，不重新发送模型工具调用。

SDK不能根据预览、工具名称或平台自行推断写权限；初始化和模型目录中没有Patch即表示该能力未安装。真实子进程/SDK回归[`test_server_and_cli.py`](../../tests/product_config/test_server_and_cli.py)证明读取、批准、文件效果和最终回答链，协议基础回归仍由[`test_server_sdk.py`](../../tests/app_server/test_server_sdk.py)覆盖。

## 45. 变更记录

| 文档版本 | 代码版本 | 日期 | 变更摘要 |
|---:|---|---|---|
| 8 | `f11359447f3bc68ffb97a100bb8b4bbcc1a891e5` | 2026-09-20 | 0.9.3a拆分子进程职责，限制Pending与Abandoned总量，增加取消安全Close和低敏资源快照 |
| 7 | `3f37fe8ae0646d3327254ce9677110b94f7c5e80` | 2026-09-19 | 同步f3物理删除Action HTTP Client、旧SDK测试和第二公共入口 |
| 6 | `pending` | 2026-09-19 | 按ADR 0081收敛SDK公共边界，撤销Action HTTP Client包级导出并把遗留实现标记为迁移兼容 |
| 5 | `71a479439edcdd29b863ec3a9bad7a52586dd1bf` | 2026-09-13 | 记录默认Patch通过既有SDK Replay、Artifact分页和审批方法完成，不新增客户端权限接口 |
| 4 | `608c548feb909aa5ae572bab7db35859283d3d01` | 2026-09-13 | 增加广告方法、协商消息字节和Replay数量的Transport写入前门禁，并登记Product UI连接恢复分层 |
| 3 | `4f7c009869a46f70169a8e34a40c1df8227a8651` | 2026-09-13 | 严格校验Response Envelope与JSON预算，限制子进程Response Frame，统一Result错误并在半握手失败后关闭且禁止复用当前连接 |
| 2 | `12f49ce60cbba09726f27ec2e9039c7c9159d67c` | 2026-09-12 | 接入Adapter现行设计，明确其只调用Submit、完整Snapshot返回及真实HTTP/LangGraph测试边界 |
| 1 | `658e04d216d7d7efb01cd2e6a9db9788917552b9` | 2026-09-12 | 建立SDK现行模块设计，覆盖双客户端边界、Transport并发取消、stdio进程、握手恢复、事件消费、HTTP资源、安全与真实测试差距 |
