# ADR 0008：采用模型无关的流式 Provider Event

- 状态：已接受
- 日期：2026-09-02

## 背景

OpenAI-compatible、Anthropic 和其他 Provider 对文本、Reasoning、Tool Call 参数、Usage、Stop Reason 和错误使用不同的流式协议。若 Agent Runtime 直接处理各 SDK 对象，会导致 Loop、Session、Protocol 和测试全部供应商化。

研究见[协议与 Provider Event 研究](../research/protocol.md)。

## 决策

实施状态（2026-09-03）：下列是目标契约范围，不等于所有事件和能力已经实现。0.3/0.4.1 实际支持的 Provider Event v1 以 `spec/provider-event-v1.schema.json` 为准；首个 Chat Adapter、有限能力和完整 Usage 边界见 ADR 0014。推理摘要、用量明细和成本继续按 0.4 迭代。

### 1. ModelProvider 端口

Runtime 只依赖：

~~~text
ModelProvider.stream(ModelRequest, CancelToken)
  -> AsyncIterator[ProviderEvent]
~~~

ModelRequest 使用供应商中立的 Model View、Tool Definitions、输出约束、能力要求和预算。认证配置通过 Secret Reference 注入，不进入请求快照。

### 2. 统一事件

v1 事件：

- response_started；
- text_started / text_delta / text_completed；
- reasoning_summary_started / delta / completed；
- tool_call_started / tool_arguments_delta / tool_call_completed；
- usage_updated；
- response_completed；
- response_failed。

每个 content/tool call 使用稳定局部 ID，Adapter 负责把任意 Chunk 边界组装为完整终值。

### 3. Stop Reason

统一为：

~~~text
completed | tool_calls | max_output_tokens | content_filter
cancelled | error | unknown
~~~

Runtime 根据语义和当前状态决定是否继续，不能把某个 Provider 的字符串直接当作 Turn 终态。

### 4. Error Taxonomy

统一错误类别：

~~~text
invalid_request | authentication | rate_limit | quota
content_policy | provider_internal | transport
invalid_provider_output | context_overflow | cancelled | unknown
~~~

错误携带 `retryable`、可选 `retry_after`、Provider/Model 标识和脱敏诊断；Runtime 最终决定是否重试。

### 5. Usage

Usage 同时表达总量和不重叠明细：

- input tokens；
- cache read/write；
- output tokens；
- reasoning tokens；
- total；
- cost estimate 与价格版本。

Adapter 必须定义供应商字段映射与不变量；未知字段进入受控 raw metadata，不能改变核心行为。

### 6. 能力协商

Provider/Model 暴露 Capability：

- stream text；
- tool calling；
- parallel tool calls；
- reasoning summary；
- structured output；
- image input；
- prompt caching；
- cancellation；
- token counting。

Context Engine 和 Runtime 在请求前校验需求，不靠运行时猜测。

### 7. 持久化边界

- Delta 默认只直播；
- Completed 内容、Tool Call、Usage 和终态映射为 AgentEvent；
- Provider 原始载荷默认不持久化；
- Debug Capture 必须显式启用、脱敏、有大小和保留期。

## Contract Test

每个 Adapter 必须通过同一套测试：

1. 文本在任意 Chunk 切分下得到相同终值；
2. Tool JSON 参数跨 Chunk 正确组装；
3. 多 Tool Call ID 不串线；
4. Usage 总量和明细满足不变量；
5. 所有 Stop Reason 和 Error 映射完整；
6. Cancel 关闭流和连接；
7. API Key、Header 和 Secret 不进入事件；
8. 非法事件顺序产生 invalid_provider_output；
9. Provider 重试不重复已经提交的 Tool Call。

## 结果

### 正向结果

- Agent Runtime 不导入具体 SDK；
- Fake/Scripted Provider 可覆盖绝大多数 CI；
- 同一会话可在语义层切换 Provider；
- 公共 Agent Protocol 不随供应商事件变化。

### 成本

- Adapter 需要维护字段映射；
- 部分供应商独有能力只能作为 Capability/Extension；
- Debug 时要同时理解原始流和归一化流。

## 被否决方案

### 直接采用 OpenAI Responses Event

覆盖 Codex 当前路径，但会把 Anthropic 和其他 Provider 降为模拟 OpenAI。

### 直接使用通用 ChatCompletion Message

无法完整表达增量 Tool 参数、Reasoning Summary、Usage 更新和结构化错误。

### 将 raw provider event 透传客户端

破坏 App Server 协议稳定性并扩大敏感信息暴露面。
