# ADR 0014：首个 OpenAI-compatible Provider

- 日期：2026-09-03
- 状态：已接受并实现，离线验收通过
- 范围：0.4.1；不表示整个 0.4 或完整 Coding Agent 已完成

## 1. 接口依据与取舍

采用可选依赖 `harnessix[openai]`，锁定环境中的官方 Python SDK 为 2.54.0。只在具体 Adapter 导入 SDK，Kernel/ModelRequest/ProviderEvent 不依赖 SDK 类型。新接 OpenAI 应用通常可选 Responses；本切片明确选择 Chat Completions，目的是支持同一协议下的第三方兼容端点，不声称覆盖 Responses、内置工具或所有 OpenAI 模型。

求证来源：

- [官方 Python 库](https://developers.openai.com/api/docs/libraries)
- [Chat Completions 创建接口](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)
- [工具调用与参数分片](https://developers.openai.com/api/docs/guides/function-calling)
- 锁定 SDK 的 `AsyncOpenAI`、`AsyncStream`、`ChatCompletionStreamOptionsParam` 实现：默认重试为 2；SDK 在 `[DONE]` 处停止；Usage 位于最后的空 choices chunk；取消必须关闭响应。

## 2. 配置、认证与生命周期

- 配置只保存端点、模型、环境变量名称、能力和预算，不保存 Key；未配置 Key 时以固定错误退出。
- 端点必须 HTTPS，不允许用户信息、查询串或 fragment；测试通过注入 HTTP Transport，不增加不安全 HTTP 开关。
- 显式传入端点和认证，禁用环境代理、重定向及 SDK 隐式重试；拒绝 SDK 的自定义环境 Header，避免把另一服务的 Header 带到兼容端点。
- Provider 是异步上下文管理器，拥有 HTTP Client；Kernel 不擅自关闭共享 Provider。退出消费、取消和异常均关闭当前响应。
- 不记录原始请求、响应、Header 或 SDK 异常；对外只返回闭集错误。此边界不是用户输入/模型输出的通用 Secret 检测器，也不保证宿主自行开启的第三方 DEBUG 日志脱敏。

## 3. 请求映射与能力

- 只支持已完成的 user/assistant/tool_call/tool_result Item；其他 Item 显式拒绝，不静默丢弃。
- 工具名以稳定哈希别名映射到函数名，避免 `file.read` 等内部名称违反供应商约束；结果 ID 使用内部 Call UUID，不能依赖跨步骤可能复用的供应商 ID。
- 并行调用合并在同一 assistant 消息中；完整校验调用/结果配对。
- 能力显式说明工具、并行工具支持，当前协议要求流式 Usage；不支持的请求在发网前失败。
- 使用明确的 `max_completion_tokens` 或兼容端点的 `max_tokens` 配置，不自动猜测模型特征。Kernel 向 Provider 传递剩余 Token 预算；该参数限制输出，不等于精确预估输入或费用。

## 4. 流状态与资源上限

- 按工具 index 拼接 JSON 参数；检查调用 ID、名称、类型、JSON object、重复键和非有限数值。
- 文本增量可实时输出；只有完整 finish reason、真实 Usage 和 `[DONE]` 都确认后才输出 ToolCallCompleted 和 ResponseCompleted。缺失 Usage 不补零；坏尾包不能触发工具执行。
- 拒绝重复结束、响应 ID 漂移、多 choices、未知工具和异常数据结构。SDK 在传输终结符后停止消费，不把终结符之后的任意字节当成新响应。
- HTTP 层约束响应字节数、分片数和 SSE frame 大小；禁用压缩以免压缩包绕过原始字节上限。另有请求字节、文本/参数和工具数量限制。
- 连接/读写超时、Provider 总时限和 Kernel Turn 总时限同时生效；取消中断连接建立、读流和重试退避。

## 5. 重试与失败

SDK `max_retries=0`。Adapter 只在尚未向 Kernel 交付语义响应事件时，对暂时网络故障、限流和服务端错误做有界退避；认证、配额、输入错误、坏协议不自动重试。流中途故障即失败，不重新开始请求，不重放工具。网络重试仍可能产生供应商侧推理费用，不承诺服务端 exactly-once。后续 b2 的尝试元数据不关闭重试边界，见 ADR 0017。

错误只包含规范 code/retryable，不传播异常原文。未获得完整 Usage 的失败请求可能已经产生费用；b2 已补充尝试与失败用量映射（[ADR 0017](0017-provider-attempt-usage.md)），计数仍不是供应商账单，成本在 0.4.3 验收。

对于已收到完整 Usage 的 length/content_filter 终态，Kernel 先记录实际用量再终结失败，不调度未完整生成的工具。`ModelRequest.remaining_tokens` 是瞬态输入，不增加持久事件版本；本切片只新增配置 Schema，不改写历史 Schema 或 Migration。

## 6. 验收

使用实际 SDK + `httpx.MockTransport` 验证文本、工具分片、多步骤 Kernel、配对、认证隔离、错误分类、有界重试、取消关闭、资源上限和异常流。共享 Provider 契约不绑定 SDK 类型。默认 CI 不访问真实模型；真实百炼兼容性、模型能力和费用只在受控 Smoke 后标记验证通过。
