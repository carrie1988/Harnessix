---
doc_type: change-design
status: current
version: 1
code_revision: f11359447f3bc68ffb97a100bb8b4bbcc1a891e5
owners:
  - core
modules:
  - app_server
  - sdk
  - session
  - protocol
  - artifacts
  - trusted_actions
  - processes
  - observability
related_adrs:
  - docs/adr/0089-bounded-local-transport-lifecycle.md
related_tests:
  - tests/app_server/test_server_sdk.py
  - tests/agent/test_session_contract.py
  - tests/protocol/test_requests.py
  - tests/artifacts/test_runtime.py
  - tests/trusted_actions/test_router.py
  - tests/processes/test_supervision_store.py
supersedes: []
---

# 0.9.3 可靠性与性能详细设计

## 1. 变更摘要

| 项目 | 内容 |
|---|---|
| 需求 | 长会话Soak、进程/数据库/客户端故障注入、并发与锁、内存、启动时延、Artifact和数据库增长基准 |
| 产品边界 | 单一Coding Agent；不恢复独立Action HTTP/Worker，不新增性能控制面服务 |
| 当前状态 | 0.9.3a已实现；0.9.3b～d尚未完成，0.9.3总项保持进行中 |
| 主要模块 | App Server、SDK、Session、Protocol Request、Artifact、Trusted Action、Process、Observability |
| 兼容级别 | 0.9.3a不改协议/数据库；b/c如需Migration必须前向升级、备份恢复和故障回滚 |
| 发布单元 | a本地传输；b持久容量；c效果恢复；dSoak与发布基线 |
| 当前实现Revision | `f11359447f3bc68ffb97a100bb8b4bbcc1a891e5` |

## 2. 需求背景与完成定义

0.9.2已经证明唯一Agent主链能在固定任务集上执行和保存证据，但“20个Trial正常结束”不能证明一个本地产品在数日
会话、大量Thread、客户端断裂、数据库增长和效果恢复竞争中仍然可靠。0.9.3必须把运行时间和故障数量当作输入，给出
可重现、可比较、可失败关闭的产品证据。

源码证据和参考项目取舍见[专项研究](../research/reliability-and-performance.md)，首个传输决策见
[ADR 0089](../adr/0089-bounded-local-transport-lifecycle.md)。

0.9.3完成必须同时满足：

1. 本地stdio/SDK对慢写、断裂、取消和关闭具有有界语义；
2. 三类核心持久数据有容量快照、保留/清理计划、崩溃恢复和备份边界；
3. Trusted Action/Process在多宿主竞争和跨Store故障下不重复效果、不把未知当成功；
4. 固定长会话场景记录峰值内存、启动时延、操作延迟、数据库/Artifact增长与清理后水位；
5. 阈值在固定环境下可重跑，原始证据低敏、完整且不选择性删除失败；
6. Linux/macOS/Windows平台边界与真实执行差异明确；
7. 设计、源码、测试、运维和发布状态同步。

## 3. 设计目标、非目标与SLO边界

### 3.1 目标

- 所有无界或长寿命集合具有明确Owner、容量或清理策略；
- 取消、超时、进程退出和数据库异常有稳定失败分类；
- 清理操作不破坏活跃Thread、Pending Command、Artifact引用或UNKNOWN效果；
- Soak报告同时保存负载、机器、Revision、样本数、分位数和失败数；
- 性能退化以版本化阈值阻断，不依赖人工观察终端；
- 生产热路径不因采集指标保存Prompt、代码、路径或Tool正文。

### 3.2 非目标

- 不在0.9.3建设云端Benchmark平台、多租户调度或公网Agent Server；
- 不承诺任意硬件上的固定绝对延迟；
- 不以Microbenchmark替代真实产品纵向Soak；
- 不在清理时改写历史业务结果或压缩审计Hash链；
- 不把SQLite替换为远程数据库作为“性能优化”；
- 不在本阶段解决安装器、升级包签名或供应链扫描；
- 不把Provider质量0/20问题归因于传输性能。

### 3.3 指标分类

| 分类 | 指标 | 解释限制 |
|---|---|---|
| 启动 | 冷启动、已有数据库启动、恢复扫描时延 | 必须绑定数据库规模和是否恢复 |
| 交互 | 创建Thread、提交Turn、Replay、列表、Artifact分页P50/P95/P99 | 不把模型网络时延混入本地存储指标 |
| 容量 | RSS峰值、Pending/Abandoned、Delta、后台Task | RSS受Python和平台分配器影响，比较需同平台 |
| 增长 | Session DB、WAL、Protocol记录、Artifact正文/索引字节 | 同时记录逻辑记录数和文件字节 |
| 清理 | 扫描数、删除数、跳过活跃数、前后水位、耗时 | Vacuum与逻辑删除分开 |
| 恢复 | 故障注入点、恢复结果、重复效果数、UNKNOWN数 | 0重复效果优先于追求自动成功 |

## 4. 总体架构

```mermaid
flowchart TB
    UI[CLI/TUI] --> SDK[Bounded Subprocess SDK]
    SDK --> STDIO[Bounded stdio Pumps]
    STDIO --> APP[Agent Protocol/App Service]
    APP --> AGENT[Agent Runtime]
    AGENT --> ACTION[Trusted Action Runtime]
    AGENT --> SESSION[(Session DB)]
    APP --> REQUEST[(Protocol Request DB)]
    AGENT --> ARTIFACT[(Artifact DB)]
    ACTION --> AUDIT[(Action Audit/Plan)]
    ACTION --> PROCESS[Process Owner]

    SNAP[Capacity Snapshot Ports] -.只读.-> SDK
    SNAP -.只读.-> APP
    SNAP -.只读.-> SESSION
    SNAP -.只读.-> ARTIFACT
    MAINT[Maintenance Plan/Executor] --> SESSION
    MAINT --> REQUEST
    MAINT --> ARTIFACT
    SOAK[Deterministic Soak Runner] --> UI
    SOAK --> SNAP
    SOAK --> EVIDENCE[(Low-sensitive Evidence)]
```

设计约束：Snapshot、Maintenance和Soak是同一产品的诊断/验证能力，不是第二套Agent执行面；它们不能直接执行Tool，
不能绕过Policy，也不能成为Session或Action结果权威。

## 5. 分片与依赖顺序

```mermaid
flowchart LR
    A[0.9.3a 本地传输] --> B[0.9.3b 持久容量]
    A --> C[0.9.3c 效果恢复]
    B --> D[0.9.3d Soak基线]
    C --> D
```

| 切片 | 交付 | 前置 | 当前状态 |
|---|---|---|---|
| a | stdio/SDK协商背压、迟到Response、取消安全Close、资源快照 | 0.9.2 | 已完成实现与本地专项验证；待本提交CI |
| b | Session/Protocol/Artifact容量合同、保留计划、清理、崩溃恢复 | a | 未实施 |
| c | Action/Process Owner fencing、孤儿扫描、Route Deadline、恢复信号 | a | 未实施 |
| d | 固定Soak负载、三平台/Container证据、阈值与发布报告 | b、c | 未实施 |

## 6. 0.9.3a接口设计与本地传输可靠性

### 6.1 组件职责

| 组件 | 职责 | 禁止行为 |
|---|---|---|
| `_StdioReader` | 从同步stdin读取单行，投递容量1 Mailbox | 解码JSON、调用Runtime、缓存多行正文 |
| `_StdioWriter` | 顺序写帧、传播空间/终结、记录单一失败 | 重排Response、吞掉写失败、持久正文 |
| `run_stdio` | 握手串行、READY并发、统一停止与关闭 | 把断线当Turn取消、重试命令 |
| `_RequestCapacity` | 约束Pending+Abandoned共享槽位 | 理解JSON-RPC业务方法 |
| `_ResponseRouter` | ID归并、迟到丢弃、Future失败 | 启动或终止进程 |
| `_ChildProcess` | 子进程、stdin写锁、Reader/Stderr Task、关闭升级 | 解析业务Result |
| `SubprocessAgentTransport` | 公共Exchange/Notify/Close和快照编排 | 自动重连或业务重试 |

### 6.2 正常请求时序

```mermaid
sequenceDiagram
    participant C as AgentClient
    participant T as SubprocessTransport
    participant K as RequestCapacity
    participant P as ChildProcess stdin
    participant R as ResponseRouter
    C->>T: exchange(frame id)
    T->>T: start once
    T->>K: reserve
    K-->>T: slot
    T->>R: register id/future
    T->>P: write + drain under lock
    P-->>R: stdout response
    R->>R: pop id
    R->>K: release slot
    R-->>T: resolve future
    T-->>C: one frame
```

### 6.3 取消与迟到Response时序

```mermaid
sequenceDiagram
    participant C as Caller
    participant T as Transport
    participant R as ResponseRouter
    participant K as Capacity
    participant S as Server
    C->>T: exchange id=1
    T->>K: reserve
    T->>R: pending[1]
    C-xT: cancel await
    T->>R: pending -> abandoned
    Note over K: slot remains occupied
    C->>T: exchange id=2
    T->>K: wait
    S-->>R: late response id=1
    R->>R: discard body and tombstone
    R->>K: release
    K-->>T: id=2 gets slot
```

取消只改变客户端等待，不证明Server没有提交事实。若Server永不响应，连接容量最终饱和，操作者或上层恢复会关闭该连接，
而不是静默遗忘身份后继续无限接收。

### 6.4 stdio停止与Writer故障

```mermaid
stateDiagram-v2
    [*] --> Reading
    Reading --> Dispatching: READY frame
    Reading --> Closing: EOF
    Reading --> Closing: writer done
    Reading --> Closing: stopping set
    Dispatching --> Reading: response enqueued
    Dispatching --> Closing: outbox timeout
    Closing --> ServiceClosed: server.close
    ServiceClosed --> RequestsSettled: gather pending
    RequestsSettled --> WriterClosed: sentinel and bounded wait
    WriterClosed --> Failed: writer error or timeout
    WriterClosed --> [*]: normal EOF
    Failed --> [*]
```

Reader/Writer守护线程承担不可取消同步调用；Event Loop只等待Mailbox/Event。Writer阻塞不会让默认线程池在解释器退出时
继续持有非守护线程。底层Write是否最终返回不影响主协程按期限失败。

### 6.5 数据结构与字段

#### `SubprocessTransportSnapshot`

| 字段 | 含义 | 敏感性/约束 |
|---|---|---|
| `state` | `not_started/running/exited/failed/closing/closed` | 低基数，不含PID |
| `max_pending_requests` | Pending+Abandoned共享上限 | 1～1024 |
| `pending_requests` | 当前等待Response数 | 不含ID |
| `abandoned_requests` | 调用方已取消、等待迟到Response数 | 不含ID |
| `stderr_tail_bytes` | 当前尾部缓冲字节数 | 不含正文，最大65536 |
| `failure_code` | Sticky稳定失败Code | 不含异常原文 |

#### `ProtocolLimits`执行点

| 字段 | Server执行点 | SDK本地执行点 |
|---|---|---|
| `maxMessageBytes` | Reader上界+Codec | Subprocess stdout StreamReader+严格Response Decoder |
| `maxPendingRequests` | READY后Semaphore | 构造时`max_pending_requests`共享容量 |
| `maxOutboundMessages` | Writer入队逻辑门禁 | 不适用，SDK每Request只等一个Response |
| `maxReplayEvents` | Service/Client现有Replay门禁 | AgentClient协商后前置校验 |

### 6.6 核心伪代码

```text
run_stdio:
  start daemon reader and writer
  while first(reader line, writer done, stopping) is reader line:
    if handshake not ready:
      process inline and enqueue responses with deadline
    else:
      lazily create semaphore from negotiated maxPendingRequests
      acquire and spawn dispatch
  set stopping; stop reader
  close server; gather accepted dispatch tasks
  close writer with sentinel and deadline
  raise writer failure or outbound timeout after cleanup
```

```text
exchange(frame):
  parse request id; start process once
  reserve shared capacity
  register future before write
  write under one lock
  await shield(future)
  on caller cancellation: pending -> abandoned, keep capacity
  on other failure before response: remove pending, release capacity
```

```text
close():
  under close lock:
    if no close task:
      mark closed; fail/wake router
      create owned child shutdown task
  await shield(the same task)
```

### 6.7 失败矩阵

| 故障 | 检测点 | 结算 | 对持久事实的影响 |
|---|---|---|---|
| stdout write/flush异常 | Writer线程 | Writer Done，主循环关闭后重抛 | 无回滚 |
| Outbox满 | Enqueue Deadline | 设置Stopping，最终Timeout | 无回滚 |
| stdin读取异常 | Reader线程 | 主循环异常进入Finally | 无回滚 |
| SDK stdout非法 | Response Decoder | 全Pending失败，容量清空 | 业务结果未知，需查询 |
| SDK stdout EOF | Reader | `server_closed` | 同上 |
| Exchange取消 | Caller Task | Abandoned占槽 | 不推断业务取消 |
| Close取消 | Caller Task | 内部Close继续 | 无 |
| Graceful退出超时 | Child Owner | Terminate → Kill | 不推断Turn结果 |

## 7. 0.9.3b持久化、事务与容量清理设计边界

0.9.3b实施前必须先形成独立ADR和Migration设计。最低合同：

### 7.1 容量快照

```text
StoreCapacitySnapshot
  store_kind
  schema_version
  database_bytes / wal_bytes
  logical_rows_by_kind
  oldest_created_at / newest_created_at
  active_rows / terminal_rows / unknown_rows
  artifact_body_bytes
```

字段不得携带Thread ID、路径、Prompt或正文。SQLite文件字节只是物理水位，不能替代逻辑记录数。

### 7.2 保留与清理计划

任何删除前先持久化不可变Plan，至少包含Cutoff、候选数、排除的活跃/UNKNOWN引用、前置Schema/数据库摘要和Dry Run结果。
执行按小事务分页，崩溃后以Plan ID恢复；Artifact删除必须先证明无Session/Action/Process引用。Vacuum是显式维护操作，
不得在Agent热路径自动运行。

### 7.3 禁删集合

- 非终态Thread/Turn；
- Pending Protocol Command及其关联业务身份；
- Pending Approval、READY/RUNNING/RECONCILING/UNKNOWN Action；
- 仍被Session Item、Action Review/Output或Process结果引用的Artifact；
- 尚未完成备份/导出的保留范围；
- 审计策略要求保留的Hash链节点。

## 8. 0.9.3c 效果恢复设计边界

本切片不扩大Tool能力，只补足已有Trusted Action/Process的Owner和恢复：

1. 产品Action Runtime启动前取得跨进程Owner Lease/Fence；
2. 旧Owner或Fence失效后不能继续提交终态；
3. 启动扫描同时检查Plan/Audit、Session、Process Lease和Artifact引用孤儿；
4. Route Deadline到期不直接判失败效果，证明不足进入UNKNOWN；
5. Reconcile每轮有界且不调用Execute；
6. 所有恢复信号使用低基数字段，原始异常经过0.9.4清洗前不得公开。

必须覆盖两个独立进程竞争、效果后崩溃、审计后Session前崩溃、Artifact发布窗口、Owner失效和数据库Busy。

## 9. 0.9.3d Soak与性能证据

### 9.1 固定场景

| 场景 | 最低负载 | 主要观测 |
|---|---|---|
| 长会话 | 单Thread连续≥1000本地确定性Turn或等价事件量 | RSS、Replay、Context/Compaction、DB增长 |
| 多Thread | ≥500 Thread，分页列表/恢复 | 启动、列表P95、对象总量 |
| SDK并发 | Pending上限附近持续请求与取消 | Pending/Abandoned、吞吐、错误数 |
| Artifact | 小件与接近单件上限的混合发布/分页/清理 | DB/正文增长、读取P95、孤儿数 |
| Action故障 | 固定故障矩阵循环 | UNKNOWN、重复效果、恢复时延 |
| 重启 | 增长前后多次冷/热启动 | 启动P50/P95、恢复扫描 |

真实Provider不是性能基线前提；默认使用确定性Provider，避免网络成本和模型抖动污染本地指标。

### 9.2 证据Manifest

必须包含Revision、平台、Python、CPU/内存摘要、场景版本、随机种子、开始/结束时间、成功/失败、样本数、分位数、
峰值RSS、前后文件水位、故障计数和各证据SHA。不得包含绝对路径、用户名、Prompt、代码、Tool正文、Secret或stderr。

### 9.3 阈值建立

首轮只冻结事实基线，不用同一数据反向挑选“刚好通过”的阈值。阈值采用基线加工程余量并在后续独立运行验证；
任何平台无数据必须标记不适用/未验证，不能填零。

## 10. 并发、锁、错误分类与可观测性

| 资源 | 现有/规划Owner | 线性化点 |
|---|---|---|
| stdio Response | `_StdioWriter` | 成功进入逻辑有界Outbox |
| SDK Request容量 | `_RequestCapacity` | Semaphore槽位取得/释放 |
| Session写 | SQLiteSessionStore | `BEGIN IMMEDIATE`事务提交+CAS |
| Protocol命令 | SQLiteProtocolRequestStore | Claim/Complete事务 |
| Artifact发布 | SQLiteArtifactStore | Body/Reference同事务 |
| Action Route | Audit Store + Router | Hash链追加和状态版本 |
| Process执行 | Process Owner Lease | Lease/Fence检查后提交 |
| Maintenance | 规划中的Store Plan | Plan先行、分页事务提交 |

获取顺序必须由各模块ADR固定；不得在持有SQLite写事务时等待模型、用户或进程退出。

## 11. 安全与隐私

- 性能和容量证据只保留计数、字节、时延和稳定分类；
- 故障注入不得对用户真实Workspace执行破坏性操作；
- Soak使用临时Workspace和独立状态根；
- Snapshot/Report禁止绝对路径、PID、Request/Thread/Action ID和正文；
- 诊断采集失败不改变Agent业务结果，但发布证据缺失必须失败关闭；
- 清理前必须备份或使用一次性夹具，生产Maintenance另行提供确认与Dry Run；
- 远程服务器不是本阶段默认依赖，本地SQLite基线先行。

## 12. 测试与验证矩阵

| 层次 | 0.9.3a现有证据 | b～d新增要求 |
|---|---|---|
| 单元 | Limit校验、容量、Snapshot、Close取消 | Capacity/Plan合同、统计重算 |
| 故障 | Writer阻塞/失败、Malformed/EOF、迟到Response | SQLite Busy/IO、崩溃点、Owner失效 |
| 纵向 | 真实stdio握手、长轮询、Product UI/Config回归 | 清理后Resume、Action/Process恢复 |
| 性能 | 当前无发布阈值 | 固定Soak和分位数证据 |
| 平台 | 本地macOS；CI包含Linux/macOS/Windows相关测试 | b/c三平台合同，d按能力运行真实场景 |
| 文档 | 研究、ADR、本文、模块设计 | 运维、验证证据、路线图关闭 |

## 13. 部署、升级与回滚

### 13.1 0.9.3a

无配置和数据库迁移。升级后SDK默认同时允许64个Pending+Abandoned；stdio按现有Protocol默认Limit运行。出现传输问题可
代码回滚，不需回滚状态数据。

### 13.2 0.9.3b/c

如增加Schema：

1. 先备份并验证Application ID、Migration连续性和Checksum；
2. Migration只做DDL/索引，不在启动事务内扫描全部正文；
3. 清理能力默认Dry Run，不因升级自动删除数据；
4. 新版本写入后不承诺旧二进制可降级打开；
5. 回滚通过恢复备份，不手写逆向Migration。

## 14. 风险与待决策

| 风险 | 当前控制 | 后续决策点 |
|---|---|---|
| 守护Writer仍阻塞 | 主协程和进程可退出 | 是否为真实OS Pipe增加平台原生Adapter |
| Abandoned占满容量 | 有界且可关闭重连 | 是否增加连接级超时/自动换代，不能静默淘汰 |
| 清理误删恢复证据 | b切片Plan先行和禁删集合 | 审计保留周期与用户导出策略 |
| Vacuum阻塞交互 | 不在热路径自动执行 | Maintenance窗口和进度/取消合同 |
| Action双Owner | c切片Fence | 锁文件、SQLite Lease或平台Mutex取舍 |
| 绝对性能波动 | 固定平台与相对阈值 | 发布硬件档位与容差 |

## 15. 源码、测试与文档映射

| 主题 | 源码 | 测试 | 当前事实源 |
|---|---|---|---|
| stdio I/O泵 | [`app_server/stdio.py`](../../src/harnessix/app_server/stdio.py) | [`test_server_sdk.py`](../../tests/app_server/test_server_sdk.py) | [App Server模块](../modules/app-server.md) |
| SDK子进程 | [`sdk/subprocess.py`](../../src/harnessix/sdk/subprocess.py) | 同上 | [SDK模块](../modules/sdk.md) |
| Agent Client | [`sdk/agent_client.py`](../../src/harnessix/sdk/agent_client.py) | App Server/Product UI测试 | [SDK模块](../modules/sdk.md) |
| Session容量 | [`session/sqlite.py`](../../src/harnessix/session/sqlite.py) | [`test_session_contract.py`](../../tests/agent/test_session_contract.py) | [Session模块](../modules/session.md) |
| Protocol历史 | [`protocol/requests.py`](../../src/harnessix/protocol/requests.py) | [`test_requests.py`](../../tests/protocol/test_requests.py) | [Protocol模块](../modules/protocol.md) |
| Artifact容量 | [`artifacts/sqlite.py`](../../src/harnessix/artifacts/sqlite.py) | [`tests/artifacts`](../../tests/artifacts/) | [Artifacts模块](../modules/artifacts.md) |
| Action恢复 | [`trusted_actions`](../../src/harnessix/trusted_actions/) | [`tests/trusted_actions`](../../tests/trusted_actions/) | [Trusted Actions模块](../modules/trusted-actions.md) |

## 16. 当前完成清单

- [x] 固定Codex/OpenCode/Claude Code逆向样本和Harnessix源码证据；
- [x] 0.9.3a ADR、正式失败语义、并发上限和资源快照；
- [x] stdio Writer故障唤醒、慢输出失败和协商Pending回归；
- [x] SDK Pending+Abandoned共享容量、迟到Response和取消安全Close；
- [x] Subprocess职责拆分和可读性门禁；
- [x] App Server、SDK、路线图与索引同步；
- [ ] 0.9.3b容量合同、Migration、清理与恢复；
- [ ] 0.9.3c Owner fencing、孤儿与Route Deadline；
- [ ] 0.9.3d完整Soak、性能阈值和冻结证据；
- [ ] 0.9.3总项关闭。
