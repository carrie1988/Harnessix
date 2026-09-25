# 第三方依赖与通知

Harnessix Code依赖第三方开源组件。各组件仍由各自权利人所有，并适用其自己的许可证；Harnessix Code的`AGPL-3.0-only`不会替换第三方许可证。

## 直接运行依赖

| 组件 | 主要用途 | 上游许可证 | 上游项目 |
|---|---|---|---|
| aiosqlite | SQLite异步访问 | MIT | [omnilib/aiosqlite](https://github.com/omnilib/aiosqlite) |
| HTTPX | HTTP客户端 | BSD-3-Clause | [encode/httpx](https://github.com/encode/httpx) |
| MCP Python SDK | Model Context Protocol Client/Server与stdio传输 | MIT | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |
| Pydantic | 数据模型与校验 | MIT | [pydantic/pydantic](https://github.com/pydantic/pydantic) |
| PyYAML | Skill Frontmatter安全YAML解析 | MIT | [yaml/pyyaml](https://github.com/yaml/pyyaml) |

## 可选运行依赖

| 组件 | 主要用途 | 上游许可证 | 上游项目 |
|---|---|---|---|
| OpenTelemetry Python | Trace与Metrics | Apache-2.0 | [open-telemetry/opentelemetry-python](https://github.com/open-telemetry/opentelemetry-python) |
| OpenAI Python | OpenAI-compatible Provider SDK | Apache-2.0 | [openai/openai-python](https://github.com/openai/openai-python) |
| Anthropic Python | Anthropic Provider SDK | MIT | [anthropics/anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python) |
| HTTPX2 | Anthropic适配器直接使用的HTTP客户端（HTTPX延续项目） | BSD-3-Clause | [pydantic/httpx2](https://github.com/pydantic/httpx2) |
| httpx2-jsfetch | HTTPX2的可选传输依赖（Pyodide/JS fetch） | BSD-3-Clause | [pydantic/httpx2](https://github.com/pydantic/httpx2) |

## 内置Eval数据

`harnessix-engineering/v1`包含三个只用于离线Coding Eval的最小派生Benchmark Archive。每个Archive均携带对应
完整`LICENSE`和`PROVENANCE.md`，Manifest固定上游Revision、来源链接、版权通知和许可证摘要；这些数据不是
Harnessix生产实现依赖，也不代表上游项目当前行为。

| 数据来源 | Benchmark用途 | 上游许可证 | 固定来源 |
|---|---|---|---|
| OpenAI Agents Python | Agent工具名、序列化、载荷与日志脱敏任务 | MIT | [Revision 8dfac2a](https://github.com/openai/openai-agents-python/tree/8dfac2aeec56f9f833b316e66f075bd888930646/src/agents/util) |
| OpenCode | 跨平台路径、重试与终端URL任务 | MIT | [Revision 69c172e](https://github.com/anomalyco/opencode/tree/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/util) |
| LangChain | 字符串化、存储清洗与批处理任务 | MIT | [Revision 54a9556](https://github.com/langchain-ai/langchain/tree/54a9556f5c89dc734f906aa084426e8a238691ac/libs/core/langchain_core/utils) |

版本解析以`uv.lock`和实际发行物为准。依赖表不替代各组件发行包中的完整许可证与版权通知，也不代表已经完成全部传递依赖审计。0.9发布门禁将生成机器可读SBOM、传递依赖许可证报告和发行物内通知集合；发现不兼容、未知或缺失许可证时必须阻断1.0发布。

Git、系统Shell、搜索工具、Container Runtime、WSL2和Docker Desktop等外部程序不由Harnessix Code发行包捆绑，其安装、使用和许可证由对应平台与用户环境管理。
