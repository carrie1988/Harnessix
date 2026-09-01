# 开发路线图

## M0：可运行 MVP

- [x] Python 3.12+ 仓库骨架；
- [x] Action Contract v1；
- [x] Tool Registry 和运行时副作用分类；
- [x] 默认 Policy 与持久化 Approval；
- [x] SQLite Effect Journal；
- [x] 执行租约和过期恢复；
- [x] 显式 `UNKNOWN` 与 Executor 对账；
- [x] FastAPI、Python SDK、LangGraph Adapter；
- [x] `system.echo`、`demo.issue.create`；
- [x] 不确定副作用与无重复恢复测试。

## M1：生产单节点

- [x] 独立 Worker 和持久化队列；
- [x] 周期性租约续期与恢复任务；
- [x] PostgreSQL Journal；
- [ ] OpenTelemetry Metrics、Trace 和结构化日志；
- [ ] OIDC Principal 认证；
- [ ] Secret Provider 与一次性注入；
- [ ] MCP Executor；
- [ ] OpenAI Agents SDK Adapter；
- [ ] 更完整的 Policy 规则和 OPA/Cedar Adapter。

## M2：安全执行与多节点

- [ ] Docker/gVisor 沙箱执行器；
- [ ] 网络出口策略；
- [ ] 多节点租约、背压和限流；
- [ ] Temporal Durable Backend；
- [ ] Compensation Contract；
- [ ] 管理员人工处置接口。

## M3：参考 Agent 平台

- [ ] LangGraph Supervisor；
- [ ] Planner、Knowledge、Operation、Reviewer Agent；
- [ ] RAG、Memory 和 Eval；
- [ ] 同一 Policy 治理 LangGraph、OpenAI 和 MCP Action；
- [ ] 故障注入与可靠性基准报告。
