---
doc_type: contract
status: historical
version: 2
code_revision: 3f75747f21dae9bb5c52d62d52a7d10815122f17
owners:
  - core
modules:
  - domain
  - api
  - adapters
  - runtime
related_adrs:
  - docs/adr/0002-unknown-first-class.md
  - docs/adr/0003-database-backed-worker-queue.md
related_tests:
  - tests/contracts
  - tests/governance/test_product_runtime_convergence.py
supersedes: []
---

# Action Contract v1

> **历史合同：** 独立Action HTTP/Worker实现已在0.9.1f3物理删除，本合同不再生成、发布或接受新请求。
> 历史Schema和实现由Git版本永久保留；当前高风险执行合同见[Trusted Actions模块](modules/trusted-actions.md)。

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
| `arguments` | Framework Adapter可先用自身Schema校验；运行时权威校验来自Tool Registry绑定的Pydantic模型。当前Action Service先创建Journal记录再执行该运行时校验，持久化边界见[Domain模块设计](modules/domain.md)与[API模块设计](modules/api.md) |
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

当前[LangChain Tool Adapter](modules/adapters.md)的`args_schema`没有与运行时Tool Descriptor版本或摘要绑定，
且Tool Call ID没有持久绑定Action ID；Framework重试、审批等待和恢复不能仅依赖Tool包装层。

## 6. 运行时 Trace Context

`ActionSnapshot` 可以包含运行时生成的 `trace_context`，使用 W3C `traceparent` 和 `tracestate`。它用于 API、持久队列和 Worker 之间的链路延续，不属于调用方的 `ActionRequest`，也不参与业务幂等指纹。

同一个 `action_id` 重复提交时返回首次创建的 Trace Context，不用新的网络请求上下文覆盖原记录。详细决策见 [ADR-0004](adr/0004-durable-trace-context.md)。
