# App Server Protocol 与 Provider Event 研究

## 1. 研究基线

见[源码研究基线](baselines.md)。本主题同时区分两条协议边界：

1. **Provider Event**：模型供应商到 Agent Runtime 的内部端口；
2. **Agent Protocol**：App Server 到 CLI、SDK、IDE 的公共协议。

两者不能复用同一事件类型，否则供应商变化会污染客户端兼容性。

## 2. 参考实现事实

### 2.1 Codex

**事实**

- App Server 使用双向、JSON-RPC-like 消息，默认 stdio JSONL；线上消息省略标准 JSON-RPC 的 `jsonrpc` 字段；
- WebSocket 被标记为实验性，服务端使用有界队列并对过载返回可重试错误；
- 连接先执行一次 initialize，再进行 thread/start、thread/resume、turn/start 等操作；
- 服务端可向客户端发送 Item Started/Delta/Completed、Turn Completed 等通知；
- Thread、Turn、Item 是协议对象，Provider 原始响应不直接暴露；
- Provider 层的 ResponseEvent 包含文本、Reasoning Summary、Tool 参数增量、完成、用量、限流和模型元数据；
- 单 Turn Model Client Session 可以在满足条件时复用 WebSocket，并尝试增量输入。

**推断**

双向连接适合审批和用户输入，因为服务端不仅推送通知，还需要发起带响应关联的请求。省略 `jsonrpc` 是 Codex 自身兼容选择，不适合 Harnessix 新协议照搬。

### 2.2 OpenCode

**事实**

- V2 Server 以 HTTP 命令端点管理 Session、Prompt、Compaction、History、Interrupt 和 Permission；
- `/api/event` 以 SSE 发送原生事件，并支持从 durable aggregate event 游标读取历史；
- PTY 使用独立 WebSocket；
- Schema 生成 OpenAPI 和 SDK；
- LLM 包定义模型无关事件：Text、Reasoning、Tool Input、Tool Call、Tool Result/Error、Step Finish、Finish 和 Provider Error；
- Usage 使用总量加互斥明细，并保留受控 raw provider metadata；
- Error 对认证、限流、配额、内容策略、传输、供应商内部错误和无效输出进行分类。

**推断**

HTTP + SSE 对 Web 客户端和资源查询友好，但审批需要额外 REST 回调，命令与事件顺序由多个连接共同维持。其 Provider Event Schema 比具体 Agent Server 传输更值得直接借鉴。

### 2.3 Claude Code 逆向仓库

**事实，仅作行为佐证**

- QueryEngine 向调用方暴露 AsyncGenerator 风格的结构化流；
- CLI 存在结构化 NDJSON 输入输出；
- 可见 SSE/WebSocket 传输相关模块。

这些文件不能证明 Claude Code 的稳定公共协议，Harnessix 不据此承诺兼容。

## 3. Provider Event Contract

Harnessix 内部 Provider Event v1：

| 事件 | 必需字段 | 说明 |
|---|---|---|
| `response_started` | response_id、model | Provider 已接受请求 |
| `text_started` | content_id | 文本块开始 |
| `text_delta` | content_id、delta | Live-only 增量 |
| `text_completed` | content_id、text | 可持久终值 |
| `reasoning_summary_started` | content_id | 公开摘要开始 |
| `reasoning_summary_delta` | content_id、delta | 不承诺原始思维链 |
| `reasoning_summary_completed` | content_id、text | 可持久终值 |
| `tool_call_started` | call_id、tool_name | Tool Call 开始 |
| `tool_arguments_delta` | call_id、delta | 参数增量 |
| `tool_call_completed` | call_id、arguments | 已完成且通过 JSON 组装 |
| `usage_updated` | input、output、cache、reasoning | 归一化用量 |
| `response_completed` | finish_reason、usage | 正常协议终点 |
| `response_failed` | ProviderError | 失败协议终点 |

统一 FinishReason：

~~~text
completed | tool_calls | max_output_tokens | content_filter
cancelled | error | unknown
~~~

统一 ProviderError：

~~~text
invalid_request | authentication | rate_limit | quota
content_policy | provider_internal | transport
invalid_provider_output | context_overflow | cancelled | unknown
~~~

错误同时包含 `retryable`、可选 `retry_after` 和脱敏诊断信息。raw metadata 只能进入受限扩展字段，不能成为 Runtime 分支条件或公共协议依赖。

## 4. Agent Protocol v1 决策

### 4.1 Envelope 与传输

- 使用标准 JSON-RPC 2.0 Envelope，保留 `"jsonrpc": "2.0"`；
- v1 默认使用 stdio 上的一行一个 JSON 对象；
- 协议版本通过 initialize capabilities 协商，不把传输版本和领域 Schema 混为一谈；
- WebSocket 只在身份认证、Origin、背压和断线恢复完成后加入；
- 文件 Artifact 走受控引用，不在 JSON-RPC 中嵌入无限大内容。

### 4.2 初始化

客户端每条连接必须先且仅先调用 `initialize`：

~~~json
{"jsonrpc":"2.0","id":"1","method":"initialize","params":{
  "protocolVersion":"1.0",
  "client":{"name":"harnessix-cli","version":"0.3.0"},
  "capabilities":{"itemDeltas":true,"serverRequests":true}
}}
~~~

服务端返回协商版本、Server 信息、支持的 Item、Tool、审批和重放能力。未初始化、重复初始化或版本不兼容返回结构化错误。

### 4.3 客户端请求

首版方法空间：

~~~text
thread/create       thread/get        thread/list
thread/resume       thread/fork       thread/archive
turn/start          turn/cancel
approval/respond
events/replay
artifact/read
~~~

所有有状态写请求携带 `requestId` 作为客户端幂等键；同键不同规范化载荷返回冲突。

### 4.4 服务端通知和请求

通知：

~~~text
thread/updated
turn/started        turn/stateChanged        turn/completed
item/started        item/delta               item/completed
item/failed         item/cancelled
usage/updated
~~~

服务端请求：

~~~text
approval/request
userInput/request
~~~

审批不能只用通知表达，因为服务端需要匹配唯一请求、超时、拒绝和断线恢复。

## 5. 顺序、重放和背压

1. 每个持久通知携带 Thread 内 `sequence`；
2. Delta 携带连接内 `streamSequence`，但不保证跨连接重放；
3. 客户端发现 sequence 缺口时调用 `events/replay` 或重新读取 Thread Snapshot；
4. Snapshot 是权威状态，Live Delta 只是低延迟展示；
5. 服务端队列必须有界；
6. 文本 Delta 可合并，终态、审批和错误不可丢弃；
7. 慢客户端超过硬上限时断开，并返回最后确认的 sequence；
8. 重连不重新执行 Turn，只重新绑定观察者。

## 6. 失败语义

| 场景 | 处理 |
|---|---|
| JSON 不可解析 | JSON-RPC parse error，不进入领域层 |
| 未知方法 | method not found |
| Schema 不兼容 | invalid params，附字段路径 |
| 重复 requestId、同载荷 | 返回原响应 |
| 重复 requestId、不同载荷 | idempotency conflict |
| 客户端断线 | Turn 按创建时运行策略继续或取消，不由传输层猜测 |
| 通知队列过载 | 合并 Delta；关键事件不可丢，必要时断开 |
| 服务端重启 | 客户端用 Snapshot + sequence 恢复 |
| Approval 超时/断线 | 保持持久等待或按显式策略过期 |

## 7. 安全边界

- stdio 模式信任父进程启动身份，但仍校验全部输入；
- WebSocket 模式必须绑定本机地址、鉴权、Origin 和消息大小；
- 客户端不能伪造 Tool Result、Policy Decision 或 Event sequence；
- Provider raw payload、API Key、环境变量和未脱敏错误不得进入协议；
- Artifact 读取重新执行 Workspace/Permission 检查；
- 协议日志默认记录方法、ID、大小和结果分类，不记录完整 Prompt。

决策分别由 [ADR 0008](../adr/0008-provider-event-model.md) 和 [ADR 0009](../adr/0009-app-server-protocol.md) 固化。

## 8. 对应测试

- JSON-RPC Schema Contract 与 Golden Transcript；
- initialize 顺序、版本和 capability negotiation；
- requestId 幂等与冲突；
- Item Delta 分块随机化后终值一致；
- Tool 参数跨任意 Chunk 边界正确组装；
- sequence 缺口、重放、断线重连；
- 慢客户端、队列满、最大消息和恶意 JSON；
- 两个 Provider Adapter 共用同一 Event Contract Test。

## 9. 源码索引

- Codex：[app-server README](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server/README.md)、[ResponseEvent](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/codex-api/src/common.rs)、[item.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server-protocol/src/protocol/v2/item.rs)
- OpenCode：[LLM events](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/llm/src/schema/events.ts)、[LLM errors](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/llm/src/schema/errors.ts)、[event group](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/protocol/src/groups/event.ts)、[event handler](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/server/src/handlers/event.ts)
- Claude 逆向仓库：[structuredIO.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/cli/structuredIO.ts)
