# Action Contract v1

## 1. 设计目标

Action Contract 是 Agent Framework 与 Harnessix 之间的稳定边界。上游框架只需要提交结构化 Action，不需要理解内部状态机、Policy 或 Journal。

## 2. 请求示例

```json
{
  "spec_version": "harnessix.action/v1",
  "action_id": "018f78cf-fb77-7b9b-8f5b-b92fe62e10c7",
  "tool": "demo.issue.create",
  "arguments": {
    "title": "订单同步失败",
    "body": "请排查订单 1001"
  },
  "principal": {
    "tenant_id": "tenant-a",
    "subject_id": "ops-agent",
    "framework": "langgraph",
    "roles": ["operator"]
  },
  "context": {
    "session_id": "thread-1",
    "run_id": "run-1",
    "trace_id": "trace-1"
  },
  "effect_hint": "idempotent_write",
  "idempotency_key": "issue:order-1001",
  "secret_refs": [],
  "metadata": {
    "adapter": "langgraph"
  }
}
```

## 3. 字段约束

| 字段 | 责任与约束 |
|---|---|
| `spec_version` | 固定为 `harnessix.action/v1` |
| `action_id` | 全局唯一 Action 身份；同一 ID 不得绑定不同载荷 |
| `tool` | 运行时注册的工具名称 |
| `arguments` | 由 Tool 的 Pydantic 模型校验 |
| `principal` | 租户、主体、框架和角色信息 |
| `context` | 上游 Session、Run 和 Trace 关联信息 |
| `effect_hint` | 调用方预期值；运行时事实来自 ToolDefinition |
| `idempotency_key` | 租户范围业务幂等键 |
| `secret_refs` | Secret 引用，不包含 Secret 值 |
| `metadata` | 非敏感扩展信息 |

## 4. 指纹

业务幂等指纹包含：

- Contract 版本；
- Tenant；
- Tool 名称；
- Arguments；
- Effect Hint；
- Secret References。

指纹故意忽略 `action_id`、Session 和 Run，因为同一个业务操作可能在框架重试或恢复时产生新的运行上下文。

## 5. 运行时 ToolDefinition

Agent 提交的 `effect_hint` 不是授权事实。ToolDefinition 由 Harnessix 运行时注册，包含：

- Tool 名称和版本；
- Pydantic 输入模型；
- 副作用类型；
- 风险等级；
- 是否强制幂等键；
- 是否强制审批；
- 是否支持对账；
- Executor 绑定。

调用方提示与运行时定义不一致时，Action 在执行前失败。

## 6. 运行时 Trace Context

`ActionSnapshot` 可以包含运行时生成的 `trace_context`，使用 W3C `traceparent` 和 `tracestate`。它用于 API、持久队列和 Worker 之间的链路延续，不属于调用方的 `ActionRequest`，也不参与业务幂等指纹。

同一个 `action_id` 重复提交时返回首次创建的 Trace Context，不用新的网络请求上下文覆盖原记录。详细决策见 [ADR-0004](adr/0004-durable-trace-context.md)。
