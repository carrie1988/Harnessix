# Harnessix

**跨 Agent 框架的副作用安全执行与治理平面。**

Harnessix 不决定 Agent 如何思考，而是治理 Agent Action 如何进入真实世界：契约校验、运行时副作用分类、策略、审批、幂等、Effect Journal、执行租约、不确定结果和外部对账。

```text
LangGraph / OpenAI Agents SDK / 自研 Agent
                     │
                Action Contract
                     │
        ┌────────────▼────────────┐
        │        Harnessix         │
        │ 校验 → 策略 → 审批       │
        │ Journal → 租约 → 执行    │
        │ UNKNOWN → Reconcile      │
        └────────────┬────────────┘
                     │
              MCP / API / DB / Shell
```

## 项目边界

Harnessix 是 Action Plane，不是完整 Agent 框架。以下能力复用现有生态：

- Agent Loop、Planning、Graph 和 Multi-Agent；
- Prompt、Memory 和 RAG；
- 模型 SDK 与模型路由；
- 通用 Durable Workflow；
- 容器或微虚拟机沙箱。

Harnessix 专注所有 Agent 框架共同面对的执行边界：外部副作用是否安全、是否可审计、发生不确定性后能否不重放原操作而完成对账。

## MVP 已实现能力

- Python 3.12+、asyncio、Pydantic v2、FastAPI；
- 版本化且框架无关的 `ActionRequest`；
- 运行时拥有的 Tool Schema、副作用类型和风险等级；
- `allow`、`deny`、`require_approval` 策略结果；
- SQLite 当前快照与追加式 Effect Journal；
- 租户范围幂等键和载荷冲突检测；
- `READY → LEASED → RUNNING` 执行租约；
- 显式 `UNKNOWN`，写操作异常默认不盲目重试；
- Executor 专用 `reconcile()` 对账契约；
- FastAPI、同步/异步 Python SDK；
- LangGraph/LangChain `StructuredTool` 适配器；
- `system.echo` 与 `demo.issue.create` 两个可运行 Executor；
- 不确定副作用注入和无重复对账测试。

## 快速开始

环境要求：Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
make install
make check
make run
```

服务默认监听 `http://127.0.0.1:8787`，交互式接口文档位于 `http://127.0.0.1:8787/docs`。

在另一个终端运行完整 MVP：

```bash
make demo
```

演示流程包括：

1. 执行只读 `system.echo`；
2. 提交 `demo.issue.create` 并进入审批；
3. 批准后模拟“外部 Issue 已创建，但本地结果丢失”；
4. Action 进入 `UNKNOWN`；
5. 对账器按业务幂等键查到既有 Issue；
6. Action 变为 `SUCCEEDED`，不重复创建 Issue。

## LangGraph 适配

```python
from pydantic import BaseModel

from harnessix import ActionContext, EffectClass, HarnessixAsyncClient, Principal
from harnessix.adapters.langgraph import HarnessixToolContext, create_harnessix_tool


class IssueInput(BaseModel):
    title: str
    body: str = ""


client = HarnessixAsyncClient()
issue_tool = create_harnessix_tool(
    action_name="demo.issue.create",
    description="创建经过治理的 Issue",
    args_schema=IssueInput,
    async_client=client,
    context=HarnessixToolContext(
        principal=Principal(
            tenant_id="demo",
            subject_id="langgraph-agent",
            framework="langgraph",
        ),
        action_context=ActionContext(session_id="thread-1", run_id="run-1"),
    ),
    effect_hint=EffectClass.IDEMPOTENT_WRITE,
    idempotency_key=lambda arguments: f"issue:{arguments['title']}",
)
```

返回的对象是标准 LangChain Tool，可直接交给 LangGraph `ToolNode`。Policy、Approval、Journal 和 Executor 仍位于 Harnessix 边界之后。

## 仓库结构

```text
src/harnessix/domain/       Action Contract、状态和端口
src/harnessix/storage/      SQLite Effect Journal 与迁移
src/harnessix/policy/       Policy Engine 实现
src/harnessix/executors/    内置和演示 Executor
src/harnessix/api/          FastAPI HTTP 边界
src/harnessix/sdk/          Python 同步/异步客户端
src/harnessix/adapters/     Agent 框架适配器
tests/                      单元和集成测试
docs/                       中文架构与决策文档
spec/                       生成的 JSON Schema 和 OpenAPI
examples/                   可运行演示
```

## 设计资料

- [总体架构](docs/architecture.md)
- [Action Contract](docs/action-contract.md)
- [Action 生命周期](docs/action-lifecycle.md)
- [自研与复用边界](docs/build-vs-buy.md)
- [开发路线图](docs/roadmap.md)

## 重要语义

Harnessix 不承诺任意外部系统上的神奇 Exactly Once。它提供的是：

> Action 身份稳定、可幂等时安全复用、结果不确定时停止盲目重试，并通过外部观察和对账尽量实现业务级 Effectively Once。
