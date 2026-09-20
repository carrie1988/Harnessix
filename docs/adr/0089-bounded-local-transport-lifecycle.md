---
doc_type: adr
status: current
version: 1
code_revision: f11359447f3bc68ffb97a100bb8b4bbcc1a891e5
owners:
  - core
modules:
  - app_server
  - sdk
  - protocol
related_adrs:
  - docs/adr/0009-app-server-protocol.md
  - docs/adr/0070-agent-protocol-v1-boundaries.md
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
related_tests:
  - tests/app_server/test_server_sdk.py
supersedes: []
---

# ADR-0089：本地Agent传输采用协商背压、迟到响应容量与取消安全关闭

## 状态

接受并由0.9.3a实施。本文只关闭本地stdio App Server与Python Subprocess SDK的传输生命周期，不表示0.9.3
持久化容量、Trusted Action故障或长会话性能基线已经完成。

## 背景

0.9.1f3已经删除独立Action HTTP/Worker，默认产品只有一条`TUI/CLI → Agent Protocol stdio → Agent Runtime`
主链。这个收敛使本地stdio不再是旁路测试适配器，而是产品进程边界；它必须对慢客户端、断裂stdout、取消请求、
迟到Response和关闭取消给出正式语义。

0.9.2关闭时的实现存在四个直接风险：

1. 同步stdout写被放入线程池，Async Task超时或取消不能终止底层阻塞写；
2. Reader等待stdin时观察不到Writer故障或Dispatch停止；
3. 握手协商的Pending/Outbound上限没有约束握手前创建的Semaphore/Queue；
4. SDK Pending与Abandoned无界，且`close()`能被调用方取消打断。

固定源码证据、参考项目取舍和剩余容量问题见
[可靠性与性能研究](../research/reliability-and-performance.md)。

## 决策驱动因素

1. 不重新引入独立网络服务、Worker或第二套Action执行面；
2. 已提交Session/Action事实不能因连接失败回滚或盲重放；
3. JSON-RPC Request取消后仍可能收到Response，迟到身份不能静默丢弃；
4. 内存容量必须包含活动Pending与取消后的Abandoned；
5. 关闭调用可以被上层取消，但Transport拥有的资源回收必须继续；
6. macOS、Linux、Windows均要使用相同公共语义；
7. 诊断不得暴露Request ID、Workspace、Prompt、Tool正文或stderr内容；
8. 实现不得让既有超大`agent_client.py`继续增长。

## 候选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 保持`asyncio.to_thread`并扩大超时 | 变更最少 | 无法终止阻塞系统调用，Writer故障仍不能唤醒Reader | 拒绝 |
| 全面改为平台原生Async Pipe | 理论上无线程 | Windows/macOS/Linux和测试BinaryIO需要多套适配，本片风险过大 | 暂不采用 |
| 守护线程I/O泵 + Event Loop有界Mailbox | 跨平台、同步流兼容、阻塞写不阻止主协程收敛 | 线程本身可能等到底层I/O返回 | 采用 |
| 取消Request立即释放容量 | 吞吐高 | 迟到Response身份丢失，连接可被未知ID击穿 | 拒绝 |
| Pending与Abandoned共享槽位 | 内存有界、迟到身份安全 | 永不响应时新Request背压到连接关闭 | 采用 |
| `close`吞掉取消并同步做完 | 调用形式简单 | 违反调用方取消，可能长期占住上层Task | 拒绝 |
| Transport创建独立Close Task并Shield | 调用方仍收到取消，资源回收继续且可再次等待 | 多一个明确所有权Task | 采用 |

## 决策

### 1. 产品边界不变

0.9.3不增加HTTP/Worker服务。`run_stdio`仍是Agent Protocol的本地传输，`SubprocessAgentTransport`仍是客户端
适配器；Agent Runtime、Session、Trusted Action和外部效果权威不迁入传输层。

### 2. App Server使用双同步I/O泵

- `_StdioReader`是唯一stdin读取者，以容量1的Async Mailbox交付完整行；
- `_StdioWriter`是唯一stdout写者，以有界Outbox顺序写帧；
- 两者使用守护线程承担不可取消同步I/O，事件循环不再把阻塞调用交给默认线程池；
- 主协程同时等待Reader、Writer终结和Stopping，任一结果进入统一关闭；
- Writer队列等待或最终排空超过期限时，`run_stdio`抛出稳定Timeout，不伪装正常EOF；
- Writer底层异常在Server和Pending收敛后重新抛出；
- 守护线程只解决退出所有权，不声明底层I/O已被强制中断。

### 3. 协商Limit必须成为执行事实

- READY前仍串行处理握手；
- Pending Semaphore只在READY后按`server.limits.max_pending_requests`创建；
- Outbox物理容量由Server上限约束，每次入队还按握手后的`max_outbound_messages`执行逻辑门禁；
- 输入和Response字节合同保持Protocol现有定义，本ADR不新增协议版本。

### 4. SDK采用共享未决容量

`max_pending_requests`同时覆盖：

```text
pending Future + abandoned request ID <= max_pending_requests
```

Request获得槽位后才注册和写入。调用方取消时，Pending变为Abandoned但不释放槽位；Reader收到迟到Response时丢弃正文并
释放槽位；Reader失败或Transport关闭时一次性失败Pending、清空Abandoned并释放全部槽位。重复ID在写入前失败并归还槽位。

### 5. 关闭由Transport拥有

第一次`close()`原子设置Closed、唤醒所有容量等待者、失败所有Pending，并创建唯一Close Task。公共调用使用
`asyncio.shield`等待该Task：调用方取消只取消自己的等待，不取消关闭stdin、优雅等待、Terminate、Kill和Reader/Stderr
结算。后续`close()`复用同一Task。

### 6. 低敏感度资源快照

`snapshot()`只返回：状态、最大未决数、Pending数、Abandoned数、stderr尾部字节数和稳定失败Code。不得返回命令、
Request ID、Response、stderr正文、PID、路径或环境变量。Snapshot不是持久恢复事实。

### 7. 模块职责拆分

Subprocess实现移入`harnessix.sdk.subprocess`：

- `_RequestCapacity`只管理槽位和失效唤醒；
- `_ResponseRouter`只管理Response身份、Future和Reader；
- `_ChildProcess`只管理进程与标准流；
- `SubprocessAgentTransport`只编排公共交换和关闭。

`agent_client.py`继续负责协议资源方法与握手，不吸收进程细节。

## 不变量

1. 同一物理stdout只有一个Writer；帧不交错；
2. Writer故障或Stopping不依赖stdin产生下一行；
3. READY后活动Request不超过协商Pending上限；
4. SDK Pending与Abandoned总数不超过构造上限；
5. 取消Request不会使其迟到Response被当作未知ID；
6. Close只创建一个资源回收Task；
7. Close开始后不再启动或写入新Request；
8. 连接故障不改变已提交Session、Action或外部效果事实；
9. Snapshot不包含高基数字段或敏感正文；
10. 传输层不执行Tool、不决定Approval、不重试业务命令。

## 失败与恢复语义

| 场景 | 公开结果 | 内存结算 | 业务恢复 |
|---|---|---|---|
| stdout写异常 | `run_stdio`在关闭后重抛原I/O异常，产品CLI统一清洗 | Writer结束、Reader停止、Server关闭 | 新进程按Session Snapshot/Replay恢复 |
| stdout长期阻塞 | 出站期限到达后`TimeoutError` | 主协程退出；守护Writer不阻挡进程 | 同上，不盲重发写命令 |
| stdin EOF | 正常关闭 | Pending收敛、Writer排空 | 可重新启动并恢复 |
| SDK Request取消 | 调用方收到`CancelledError` | ID进入Abandoned并继续占槽 | 查询业务事实；迟到Response只释放槽位 |
| SDK Reader损坏/EOF | 全部Pending收到稳定错误 | Pending/Abandoned清空，等待者唤醒 | 新建Transport并按原业务身份查询 |
| SDK Close等待被取消 | 调用方收到`CancelledError` | 内部Close Task继续 | 可再次调用`close`等待完成 |
| 子进程不退出 | 优雅期限后Terminate，再超时Kill | Reader/Stderr最终Join | 不推断业务回滚 |

## 后果

### 正面后果

- 本地产品不再依赖默认线程池中不可控的阻塞stdin/stdout任务；
- 协商资源上限从“文档字段”变为真实执行门禁；
- 高取消或永不响应场景的SDK内存受到固定容量约束；
- 调用方取消关闭不再泄漏直接子进程；
- 可在不泄漏正文的前提下观察Pending、Abandoned和生命周期；
- SDK职责拆分满足0.9.0可读性门禁。

### 负面后果

- 阻塞的第三方BinaryIO调用仍可能让守护线程存活到进程结束；Python无法通用强杀线程；
- Abandoned永不返回时会占用槽位，连接可能停止接收新Request，必须关闭/重连；
- Close仍只拥有直接子进程，不拥有任意后代进程树；
- stdio超时现在以失败退出，不再静默当作正常EOF，运维需按传输故障诊断；
- Snapshot是拉取式本地数据，尚未接入统一Metric/Trace。

## 兼容、迁移与回滚

- Agent Protocol版本、JSON字段、Session Schema和配置文件均不变化；
- `SubprocessAgentTransport`保留原构造方式，新增参数均有兼容默认值；
- `harnessix.sdk`公共导出集合不变化，原`agent_client`导入路径继续可用；
- 回滚只需回退代码，不涉及数据库降级；
- 若守护线程泵在某平台出现不兼容，可回滚本切片，但不得保留“已完成0.9.3a”的路线图状态。

## 验证要求

- 协商Pending=1时，长轮询必须阻塞后续Request，释放后继续；
- stdout阻塞必须在期限内失败且Session无损；
- stdout失败时stdin保持打开，Server仍须退出；
- 取消Request必须转为Abandoned，迟到Response释放槽位，下一Request继续；
- 取消外层Close等待后，再次Close能观察同一关闭完成；
- Malformed、超大和乱序Response既有回归继续通过；
- Ruff、Mypy、可读性门禁、全仓测试和三平台CI通过。

## 未由本ADR解决

Session/Protocol/Artifact保留与Vacuum、全局Delta容量、Trusted Action Owner fencing、跨Store孤儿、进程后代、
远程Agent Protocol、统一Telemetry以及长会话性能阈值仍由0.9.3b～d、0.9.4和0.9.5处理。
