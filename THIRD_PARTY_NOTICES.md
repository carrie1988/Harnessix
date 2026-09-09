# 第三方依赖与通知

Harnessix Code依赖第三方开源组件。各组件仍由各自权利人所有，并适用其自己的许可证；Harnessix Code的`AGPL-3.0-only`不会替换第三方许可证。

## 直接运行依赖

| 组件 | 主要用途 | 上游许可证 | 上游项目 |
|---|---|---|---|
| aiosqlite | SQLite异步访问 | MIT | [omnilib/aiosqlite](https://github.com/omnilib/aiosqlite) |
| asyncpg | PostgreSQL异步访问 | Apache-2.0 | [MagicStack/asyncpg](https://github.com/MagicStack/asyncpg) |
| FastAPI | HTTP API | MIT | [fastapi/fastapi](https://github.com/fastapi/fastapi) |
| HTTPX | HTTP客户端 | BSD-3-Clause | [encode/httpx](https://github.com/encode/httpx) |
| MCP Python SDK | Model Context Protocol Client/Server与stdio传输 | MIT | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |
| Pydantic | 数据模型与校验 | MIT | [pydantic/pydantic](https://github.com/pydantic/pydantic) |
| Uvicorn | ASGI Server | BSD-3-Clause | [encode/uvicorn](https://github.com/encode/uvicorn) |

## 可选运行依赖

| 组件 | 主要用途 | 上游许可证 | 上游项目 |
|---|---|---|---|
| LangChain Core | LangGraph Adapter接口 | MIT | [langchain-ai/langchain](https://github.com/langchain-ai/langchain) |
| OpenTelemetry Python | Trace与Metrics | Apache-2.0 | [open-telemetry/opentelemetry-python](https://github.com/open-telemetry/opentelemetry-python) |
| OpenAI Python | OpenAI-compatible Provider SDK | Apache-2.0 | [openai/openai-python](https://github.com/openai/openai-python) |
| Anthropic Python | Anthropic Provider SDK | MIT | [anthropics/anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python) |

版本解析以`uv.lock`和实际发行物为准。上述表格记录直接依赖，不替代各组件发行包中的完整许可证与版权通知，也不代表已经完成全部传递依赖审计。0.9发布门禁将生成机器可读SBOM、传递依赖许可证报告和发行物内通知集合；发现不兼容、未知或缺失许可证时必须阻断1.0发布。

Git、系统Shell、搜索工具、Container Runtime、WSL2和Docker Desktop等外部程序不由Harnessix Code发行包捆绑，其安装、使用和许可证由对应平台与用户环境管理。
