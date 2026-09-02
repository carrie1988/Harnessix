# ADR 0009：App Server v1 使用标准 JSON-RPC 2.0 与 stdio JSONL

- 状态：已接受
- 日期：2026-09-02

## 背景

Harnessix Code 需要让 CLI、后续 TUI、SDK 和 IDE 驱动同一 Runtime。协议必须支持：

- Thread/Turn/Item 命令与流式事件；
- 服务端发起 Approval/User Input 请求；
- 客户端断线和事件续传；
- Headless 使用；
- Schema 版本与兼容性；
- 有界背压。

Codex 使用双向 JSON-RPC-like stdio，OpenCode 使用 HTTP + SSE 并为 PTY 使用 WebSocket。两种方案都有效，但第一版 Harnessix 是本地优先 CLI + Headless Runtime。

## 决策

### 1. 标准 Envelope

使用标准 JSON-RPC 2.0：

- 保留 `jsonrpc: "2.0"`；
- Request、Response、Notification 遵循标准关联规则；
- 领域错误放入结构化 error data；
- 不复用 Provider Event 名称。

### 2. 第一版传输

默认使用 stdio JSONL：

- 一行一个 UTF-8 JSON 对象；
- stdout 只输出协议消息；
- 诊断日志写 stderr；
- 单条消息和队列大小有硬上限；
- 不在消息中嵌入大型二进制或完整日志。

WebSocket 推迟到 0.8，在鉴权、Origin、重连和背压完成后使用同一 Envelope。

### 3. 初始化与能力

每条连接必须先且仅先调用 initialize。双方协商：

- Protocol 版本；
- Item/Delta 支持；
- Server Request 支持；
- Artifact 传输；
- Replay Cursor；
- 最大消息与队列限制。

不兼容版本在执行任何领域命令前失败。

### 4. 方法空间

v1 客户端请求：

~~~text
thread/create, thread/get, thread/list, thread/resume
thread/fork, thread/archive
turn/start, turn/cancel
approval/respond
events/replay
artifact/read
~~~

服务端通知：

~~~text
thread/updated
turn/started, turn/stateChanged, turn/completed
item/started, item/delta, item/completed, item/failed, item/cancelled
usage/updated
~~~

服务端请求：

~~~text
approval/request
userInput/request
~~~

### 5. 幂等、顺序和重放

- 有状态写请求必须包含 requestId；
- 同 requestId 同载荷返回原结果，不同载荷返回冲突；
- 持久通知携带 Thread sequence；
- Delta 只保证单连接内顺序；
- 客户端发现缺口后请求 replay 或完整 Snapshot；
- 断线不会默认取消 Turn，行为由 turn/start 的 run policy 决定。

### 6. 背压

- 每个连接使用有界队列；
- Text/Reasoning/Progress Delta 可合并；
- Turn/Item 终态、Approval、Error 不可丢弃；
- 超过硬上限时断开慢客户端并返回最后可恢复 sequence；
- Runtime 不等待 UI 消费 Token 才继续持久状态转换。

### 7. Schema

- 使用 Pydantic 模型生成 JSON Schema；
- Protocol Schema 单独版本化并提交仓库；
- 新增可选字段保持向后兼容；
- 删除、重命名或改变语义需要主版本；
- 未知通知可忽略，未知请求必须返回 method not found。

## 安全

- stdio 仍校验所有客户端输入；
- 客户端不能直接提交 AgentEvent、ToolResult 或 PolicyDecision；
- Artifact 读取重新鉴权；
- 消息、字符串、数组深度和日志内容有上限；
- Prompt、Secret、Provider Header 默认不记录；
- WebSocket 模式上线前必须有本机鉴权和 Origin 防护。

## 结果

### 正向结果

- 双向审批自然表达；
- CLI 和 Headless 共用协议；
- 标准工具可以理解 Envelope；
- stdio 实现简单且不提前承担网络服务攻击面。

### 成本

- 浏览器客户端需要后续 WebSocket Gateway；
- 资源查询不如 REST 天然；
- Server Request 要求客户端实现双向调度；
- JSONL 必须严格隔离 stdout 日志。

## 被否决方案

### 自定义 Envelope

没有足够收益抵消 Schema、错误和工具生态的重复设计。

### 第一版 HTTP + SSE

适合 Web，但会增加端口、认证、双连接排序和审批回调复杂度。

### 第一版直接 WebSocket

本地 CLI 不需要，且会过早引入网络安全与断线状态。
