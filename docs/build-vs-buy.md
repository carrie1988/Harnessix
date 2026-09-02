# 自研与复用边界

Harnessix Code 自研决定 Coding Agent 行为、可靠性和差异化的核心运行语义；成熟标准、协议、系统工具和供应商 SDK 优先复用。

## 1. 决策表

| 能力 | 决策 | 原因 |
|---|---|---|
| Agent Loop 与 Turn 状态机 | 自研 | 核心学习目标，决定取消、恢复、工具循环和终止语义 |
| Thread/Turn/Item 领域模型 | 自研 | 公共协议、持久化和客户端体验的共同基础 |
| Agent Event Protocol | 自研契约，复用 JSON/JSON-RPC 等标准 | 需要客户端无关和版本兼容，但不应发明传输基础设施 |
| Provider 统一端口 | 自研 | 隔离供应商差异，稳定 Agent Runtime |
| 模型 HTTP/SDK 调用 | 复用官方 SDK 或可靠 HTTP 客户端 | 避免维护认证、传输和供应商细节 |
| Context Engine 与 Compaction 策略 | 自研 | Coding Agent 质量和可解释性的核心 |
| Coding Tool Contract/Registry | 自研 | 决定参数、结果、错误、权限和并发语义 |
| `grep`、Git、Shell 等底层程序 | 复用系统工具或成熟库 | 不重新实现搜索、版本控制和 Shell |
| Patch 事务与 Diff 语义 | 自研边界，复用成熟解析库需评估 | 必须保护用户工作区和支持失败恢复 |
| Process Runtime | Python-first 自研适配；必要时 Rust Sidecar | 需要统一超时、取消、进程树和输出语义 |
| 容器与系统 Sandbox | 复用 Docker、gVisor、bubblewrap 等 | 不自行实现内核隔离 |
| Permission/Approval | 自研 | 与 Tool、Workspace、Agent Turn 和 Action Plane 深度关联 |
| Action Plane | 自研 | Harnessix 的差异化：幂等、Effect Journal、`UNKNOWN`、对账 |
| MCP | 复用官方协议 SDK，自研接入边界 | 协议无需重写，但扩展不能绕过安全策略 |
| Skills/项目指令/Hooks | 自研发现和生命周期语义 | 需要与 Context、Tool 和 Permission 集成 |
| Session 数据库 | 自研 Repository，复用 SQLite/PostgreSQL | 领域 Schema 自有，存储引擎复用 |
| 通用 Durable Workflow | 暂不引入；出现分布式需求后评估 Temporal | 第一版本地 Agent 不需要额外工作流复杂度 |
| 遥测 | 复用 OpenTelemetry，自研 Agent 语义约定 | 标准传输与自有领域指标结合 |
| 身份认证 | 本地模式复用 OS；服务模式复用 OAuth/OIDC | 不自创身份协议 |
| CLI 参数解析 | 复用标准库或成熟库 | 不构成差异化 |
| TUI 渲染 | 评估成熟框架 | 聚焦 Agent 交互模型而非终端绘制底层 |
| Evals Runner 与任务数据模型 | 自研核心，复用测试/容器工具 | 需要与 Harnessix 事件、成本和任务结果关联 |

## 2. 不再依赖 LangGraph 作为核心

LangGraph、OpenAI Agents SDK 等仍可以通过 Adapter 使用 Harnessix Action Plane 或 Agent Protocol，但 Harnessix Code 的核心 Agent Loop、Session 和 Tool Runtime 不建立在这些框架之上。

原因：

- 核心目标是理解和拥有 Coding Agent 运行语义；
- 第三方框架升级不能决定 Harnessix 的持久化和恢复兼容性；
- Coding Agent 的流式 Item、Shell、Patch、Context 和审批行为需要更精确的领域模型；
- Adapter 应位于边界，不应成为核心依赖方向。

## 3. 引入新依赖的门槛

新增依赖前必须回答：

1. 该问题是否已有当前依赖或标准库能够解决；
2. 该依赖是否位于核心持久化或安全路径；
3. 失败、取消和资源释放语义是否满足要求；
4. 是否支持 macOS/Linux 和 Python 3.12+；
5. 维护活跃度、许可证、供应链和版本锁定风险如何；
6. 能否通过端口隔离，未来替换而不破坏领域模型；
7. 引入后减少的代码和新增的复杂度是否值得。

只有经过 ADR 或依赖评审后，新的框架级、持久化、安全和协议依赖才能进入核心。
