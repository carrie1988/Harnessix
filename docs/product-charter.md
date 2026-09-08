# Harnessix Code 产品章程

## 1. 产品定义

**Harnessix Code**通过研究Codex、OpenCode、Claude Code等主流Coding Agent的架构、公开行为和可核验实现思路，独立设计并实现面向真实软件工程任务的、本地优先、模型无关、安全可控、可恢复、可审计、可评测、可扩展的生产级Coding Agent。

它面向真实软件仓库完成理解、规划、修改、命令执行、验证、审查和结果交付，并把模型推理、上下文管理、工具执行、权限审批、会话恢复和副作用治理纳入同一个可观测、可测试的运行时。1.0不是POC、功能演示或仅供二次开发的Runtime库，而是能够正式安装、升级、诊断和长期使用的商业版本。

仓库名、Python 包名和 CLI 命令继续使用 `Harnessix` / `harnessix`。原有 Framework-agnostic Agent Action Plane 不再作为顶层产品，而是作为 Harnessix Code 的执行治理子系统继续演进。

## 2. 目标用户

第一阶段目标用户是：

- 希望在macOS/Linux本地代码仓库中长期使用可控Coding Agent的独立开发者；
- 需要接入不同模型供应商，又不希望业务绑定单一模型 SDK 的团队；
- 对命令执行、文件写入、网络访问和外部系统副作用有审计与审批要求的工程团队；
- 需要研究和扩展 Agent Loop、Context、Tool、Sandbox、MCP、Skills 的 Agent 工程师。

## 3. 1.0产品形态与规模边界

1.0采用以下约束：

- 本地优先，支持 macOS 和 Linux；
- 提供CLI/TUI、无界面的App Server和Python Agent SDK；
- 单个 Workspace 对应一个明确的文件系统边界；
- 支持交互式会话和一次性 Headless 任务；
- 支持 OpenAI-compatible 与 Anthropic 两类 Provider；
- 支持读取、搜索、通用进程、多文件事务修改、Shell、Git、测试、Checkpoint和Rollback闭环；
- 支持持久会话、恢复、取消、审批和上下文压缩；
- 支持 MCP、项目指令和 Skills；
- 支持Host安全级别与至少一种Container隔离执行后端；
- 高风险外部副作用由 Harnessix Action Plane 治理。

1.0面向大量相互独立的本地终端实例，规模能力体现为发行物可重复安装、兼容升级、稳定运行、故障恢复、问题诊断和质量回归，不表示集中式多租户SaaS。IDE、Web、远程Sandbox、云任务、多租户控制面和大规模分布式调度进入1.x候选范围，但核心协议和执行端口不得阻断后续演进。该边界见[ADR 0062](adr/0062-local-first-v1-commercial-boundary.md)。

## 4. 核心价值

### 4.1 完整 Coding Agent 闭环

Harnessix Code 必须能够独立完成：

```text
理解请求 → 探索仓库 → 制定或调整计划 → 调用工具
→ 修改代码 → 运行验证 → 根据结果继续迭代 → 交付变更摘要
```

核心 Agent Loop 不依赖 LangGraph 等通用编排框架，避免把关键运行语义交给第三方框架。

### 4.2 生产级执行安全

对文件写入、Shell、网络访问和外部系统操作进行分级处理：

- 只读操作走低开销执行路径，但仍保留结构化事件；
- Workspace 内写操作必须可生成 Diff、可审批、可取消；
- Shell 具备超时、输出限制、进程树终止和资源清理；
- 外部不可逆操作进入 Action Plane，使用策略、审批、幂等和 Effect Journal；
- 不确定副作用不得盲目重试，必须进入显式对账流程。

### 4.3 可恢复、可解释、可评测

- Thread、Turn、Item 和运行事件可持久化；
- 进程退出后能够恢复到明确状态；
- Context 的来源、裁剪和压缩结果可检查；
- Tool Call、审批、执行结果和最终代码 Diff 可追踪；
- 通过确定性 Fake Provider、Transcript Replay 和真实仓库 Eval 防止回归。

### 4.4 Provider 与客户端解耦

- Agent Runtime 面向统一模型事件，而不是供应商原始响应；
- App Server 面向版本化协议，而不是绑定某个 CLI/TUI；
- Provider、Tool、Sandbox、Session Store 和扩展机制均通过稳定端口接入。

## 5. 明确不做

1.0不做以下事情：

- 不复制通用工作流引擎；
- 不以复杂多 Agent 拓扑代替可靠的单 Agent Loop；
- 不把 RAG、向量数据库或长期记忆作为 Coding Agent 的默认前提；
- 不追求一次支持所有模型、IDE 和操作系统；
- 不在1.0同时建设远程执行、多租户、计费和分布式调度控制面；
- 不把 Prompt 中的文字约束冒充真正的权限隔离；
- 不承诺任意外部系统上的 Exactly Once；
- 不直接复制参考项目实现代码；任何代码复用必须满足许可证和归属要求，研究记录固定版本、来源和独立架构决策。

## 6. 产品成功标准

Harnessix Code 1.0 必须满足：

1. 能在非示例仓库中完成“理解—规划—多文件修改—执行—测试—审查—Git交付”的闭环；
2. 支持交互式与 Headless 两种运行方式；
3. 支持至少两个 Provider 家族，并通过统一契约测试；
4. 支持会话恢复、用户取消、Tool 超时和上下文压缩；
5. 具备Workspace边界、通用进程监督、命令审批、网络策略、Secret最小化注入和至少一种隔离执行后端；
6. 支持 MCP、项目指令和 Skills；
7. 有稳定、版本化的 App Server 协议；
8. 有单元、契约、集成、端到端、故障注入和真实仓库 Eval；
9. 提供macOS/Linux安装、跨版本升级、备份恢复、卸载、配置、诊断和安全文档；
10. 提供用户数据导出、删除、保留及诊断脱敏能力；
11. 发布可复现的质量、成本、延迟、任务成功率和人工干预率基线；
12. 通过长会话Soak、故障注入、安全测试和受控真实用户Dogfooding门禁。

## 7. 项目能力摘要

项目完成后应能够被准确描述为：

> 独立设计并实现生产级 Coding Agent Runtime，包含持久 Agent Loop、Provider 抽象、上下文压缩、代码工具、进程与沙箱、权限审批、MCP/Skills、双向事件协议和自动化评测；通过 Action Plane 进一步解决外部副作用的幂等、恢复与对账问题。

这一定义同时体现 Coding Agent 的完整性和 Harnessix 独有的执行治理能力。
