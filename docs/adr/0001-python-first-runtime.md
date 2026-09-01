# ADR 0001：采用 Python-first Runtime

- 状态：已接受
- 日期：2026-09-01

## 背景

Harnessix 第一阶段需要快速接入 LangGraph、OpenAI Agents SDK、MCP 和企业 Python Agent 应用。此前误建的 Rust Core 会增加跨语言契约、构建和调试成本，且尚无性能数据证明必须使用 Rust。

## 决策

第一阶段统一使用 Python 3.12+、asyncio、Pydantic v2、FastAPI 和 SQLite。通过领域层、应用服务层、基础设施层和框架适配器的依赖边界保持核心框架无关。

## 结果

- 更快验证 Action Contract 和 Effect Safety 语义；
- 直接复用主流 Agent Python 生态；
- 避免过早引入跨语言复杂度；
- 后续只有在可测量的性能或隔离需求出现时，才将局部 Executor 下沉到 Rust、Go 或独立进程。
