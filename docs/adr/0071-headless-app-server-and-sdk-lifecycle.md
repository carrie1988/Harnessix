---
doc_type: adr
status: current
version: 1
code_revision: 401759ebc42e5c8440e66f25d528a01c3bc68ed1
owners:
  - core
modules:
  - app_server
  - sdk
related_adrs: []
related_tests:
  - tests/app_server
  - tests/unit/test_sdk.py
supersedes: []
---

# ADR 0071：Headless App Server与Agent SDK生命周期

- 状态：已接受
- 日期：2026-09-09
- 实施：0.8.2

## 背景

0.8.1已经冻结Agent Protocol v1、公共投影、Replay游标和持久命令账本。产品入口仍缺少可执行的stdio服务和客户端SDK。这里最危险的不是方法转发，而是命令受理、领域提交、响应、后台执行和进程退出之间存在多个崩溃窗口；若把JSON-RPC响应、内存任务或实时连接当成事实，重连可能丢Turn或重复副作用。

## 决策

1. 首个Headless传输仅支持单客户端stdio JSONL。stdout只写协议帧，诊断只允许写stderr；TCP、HTTP和WebSocket不属于0.8。
2. `AgentApplicationService`是协议到既有`AgentRuntime`和`SessionStore`的应用层，不复制Agent状态机，不允许SDK直接读取SQLite。
3. 创建Thread、开始Turn和重试Turn使用稳定`clientInstanceId + requestId`推导领域身份。命令顺序固定为：协议账本`accepted`、领域接受事实、协议账本`completed`、后台驱动。后台驱动不得早于命令结果持久化。
4. Agent Event/Thread v18在`TurnStarted`和Turn投影中持久化`executionMode=immediate|deferred`。只有App Server的延迟驱动Turn可在宿主重开时保持`ACCEPTED`；进程内`run_turn`的旧崩溃语义仍进入`INTERRUPTED`。Session migration21只登记这一向前兼容契约，不改写历史事件或快照。
5. 若宿主在协议结果提交后、后台调度前退出，延迟驱动的`ACCEPTED` Turn保持可恢复。相同命令重放或`thread/resume`会调度同一Turn；单服务按`turnId`去重后台任务，Runtime仍以活动Turn检查作为第二道防线。
6. 事件消费只以`events/replay`和`scannedThrough`为恢复事实。0.8.2不承诺实时Delta；0.8.3可增加通知，但不得改变Replay权威性。
7. 连接严格执行`new → initialized_pending_ack → ready → closing → closed`。协商后的消息、Replay、待决请求和出站容量取客户端与服务端上限的较小值。
8. 出站只有一个Writer。队列达到协商上限且在界限内不能排空时关闭连接，不阻塞Runtime持久化线程，也不丢弃或伪造领域事件。
9. EOF触发有界优雅关闭：先拒绝新请求，等待后台Turn到界限；超时则取消后台任务，由Runtime提交确定性取消终态。输出端损坏必须结束服务而不是继续吞入命令。
10. Python Agent SDK同时提供进程内和子进程传输。Notification使用只写路径，禁止等待不存在的Response；Request继续要求恰好一个匹配ID的Response。SDK重连由调用方复用稳定`clientInstanceId`、领域`requestId`和Replay游标完成。
11. `thread/create.workspace`必须是App Server宿主语义下的无NUL绝对路径。该字段是Thread绑定，不授予文件权限；后续Action仍须经过0.7 Workspace、Permission和Sandbox验证。

## 失败语义

| 场景 | 结果 |
|---|---|
| 账本accepted后、领域提交前退出 | 相同命令重放原操作；确定性领域身份防止重复 |
| 领域接受后、账本completed前退出 | 相同命令返回或完成同一领域对象，不创建第二个对象 |
| 账本completed后、后台调度前退出 | 重放命令或恢复Thread调度同一ACCEPTED Turn |
| 客户端未消费事件即断线 | 以最后`scannedThrough`重新Replay |
| Notification收到Response | SDK以`invalid_response`失败关闭，不猜测帧身份 |
| stdout阻塞或损坏 | 有界关闭连接；Session事实保持可重放 |
| EOF时Provider仍未返回 | 宽限期后取消，Runtime提交`CANCELLED` |

## 取舍

- 单客户端stdio避免在本阶段引入端口认证、Origin和多租户资源隔离，但不提供远程访问。
- 0.8.2的SDK为异步Python接口；同步包装、完整CLI体验和发布安装器分别在0.8.3与0.9收口。
- Thread列表当前由Session端口读取投影并在分页前过滤，优先保证游标准确；大量Thread索引和查询性能基准属于0.9可靠性工作。

## 验证

- 握手状态、版本、未知参数和错误映射；
- 相同命令、参数漂移、三处崩溃窗口和Runtime重启；
- Snapshot加Replay断线续传；
- 归档过滤先于分页；
- 子进程Notification只写语义；
- 慢输出、EOF和长Provider下的有界关闭及Session完整性。
