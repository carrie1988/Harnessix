# 自研与复用边界

Harnessix 只拥有跨框架 Action 治理所必需的语义。

| 能力 | 决策 |
|---|---|
| Agent Loop、Graph、Workflow | 复用 LangGraph、OpenAI Agents SDK 等 |
| Model SDK 和模型路由 | 复用供应商 SDK 或 Gateway |
| Memory、RAG、Prompt | 由 Agent 应用层选择 |
| MCP 协议 | 复用官方或社区 SDK |
| 通用 Durable Workflow | 后续集成 Temporal，不自行复制 |
| 容器/微虚拟机沙箱 | 后续集成 Docker、gVisor、Firecracker 或 E2B |
| 遥测协议 | 复用 OpenTelemetry |
| 身份协议 | 复用 OAuth/OIDC |
| 框架无关 Action Contract | Harnessix Core |
| 运行时副作用分类 | Harnessix Core |
| Policy 与 Action Approval | Harnessix Core |
| Effect Journal 与 `UNKNOWN` | Harnessix Core |
| Executor Reconciliation | Harnessix Core |
| 生命周期不变量 | Harnessix Core |

新增能力只有在能够跨框架保护或执行 Action 时，才应进入 Harnessix Core。
