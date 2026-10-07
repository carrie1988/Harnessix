---
doc_type: validation-evidence
status: current
version: 1
code_revision: 7fca4a526bbbd3cde7e7c66552704126757171c2
owners: [core]
modules: [sdk, product_config, agent, evals]
related_adrs:
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/app_server/test_server_sdk.py
  - tests/evals/test_provider_budget_period_isolation.py
supersedes: []
---

# BETA-001 初始源码限定只读分析执行记录

## 1. 结论与范围

2026-10-07，真实Harnessix嵌入式SDK协议对12件经审阅的初始未整改源码启动一次只读分析Turn。
四个百炼模型请求已发送并结算为已知估算，原Turn以`budget_exceeded`失败，没有完成源码分析。
原执行收据未观察到成功的`read_file`结果。后继原认证事件只读诊断确认四次工具均为逐级`list_files`，
依次读取`.`、`backend`、`backend/src`、`backend/src/main`，全部成功；没有参数错误、未知工具、失败重试或`read_file`调用。
直接失败是步骤耗尽，导航效率缺口与原一级目录概览共同导致没有进入源码读取；不能将模型或工具成功终结等同业务分析完成。
该次失败不自动重跑，不放宽原预算，也不以新结果覆盖原失败。

这不是完整登录整改、默认stdio CLI/TUI验收、隔离业务测试或Beta PASS；真实任务完成数仍为0。
任务完整契约见[任务登记与验收方案](../../operations/pilot-tasks/001-login-password-protection.md)。
本资料不修改产品实现、任务评分规则或R3/R4/R5/R6发布门禁。

## 2. 候选、输入与协议链

| 项目 | 实际执行身份及边界 |
|---|---|
| 候选Revision | `7fca4a526bbbd3cde7e7c66552704126757171c2` |
| 独立安装 | `1.0.0rc1` Wheel，CPython 3.12.7；507件Python源码与候选一致，不是可编辑安装 |
| Wheel SHA256 | `731396d93ed1f2e8ee21e1060531bf5ae44217d82e790767a8acb0e20c8fedde` |
| 验证宿主SHA256 | `ce91b242c7180711cafd776074e2b7d930c080bef93e59853cc96bb9358b2649`；私有一次性宿主，不进入Wheel |
| 输入范围 | 12文件、50,712字节，经审阅的初始源码独立副本；无参考整改源码、补丁或答案输入 |
| 完整业务副本 | 与最小分析输入分开；419文件分类不能推导全部可外发、可构建或可验收 |
| Provider | 北京兼容端点，固定日期模型`qwen3-coder-plus-2025-09-23` |
| Turn预算 | 4步、16,000 Token、180秒；单响应输出1,024 Token、一次尝试、零重试 |
| 实际运行 | 一次Turn、4个请求；宿主实际退出1，不自动重跑 |

协议路径为`AgentClient → InProcessAgentTransport → AgentProtocolServer → AgentApplicationService → AgentRuntime`。
真实产品Session保护、SQLite状态、Artifact Reader、请求Store和WorkspaceScope复用既有实现；Provider通过原Guard消费唯一新周期账本。
宿主使用共享产品Context指令，不接收完整整改参考结果。嵌入式协议运行不冒充默认stdio进程或TUI安装验收。

广告工具只有`list_files`、`read_file`、`glob`、`grep`和`read_artifact`，没有Patch、Process、MCP、Skill或Hook。
宿主审计钩子不是操作系统沙箱；只读权限依赖原WorkspaceScope及独立副本边界，不能据此宣称OS强隔离。

## 3. 费用事实

R3、BETA-001及其他百炼验证共用[独立60元周期](../../changes/m09-provider-budget-period-activation.md)，不是每个用途分别60元。
四条新请求均为`completed`，用量价格估算合计`0.072852`元，预留`0`，没有新增unknown。
实际供应商账单未确认，账单金额为null而不是0；观察时剩余估算为`59.927148`元。
四个Attempt与四个持久预留身份一一绑定，调用顺序、终态和关联信息保存在受控收据中。

旧周期两条unknown及其全额预留仍保留原字节，旧原件摘要核对为
`d923d8e8812e9706229b290f362b472f3934086feb75a908534cd415f88b4385`。
它们不计入新额度、不再阻塞新路径，但不能认定实际费用为0；新周期新增reserved/unknown仍停止后续请求。

## 4. 输入完整性、持久化与重开

12件分析输入的前后SHA256和文件身份一致。该证明只覆盖本次最小分析副本，不包含完整原项目正文、业务状态或生产部署。
原项目既有快照只对部分允许文件取得正文摘要，其余有元数据覆盖，不能宣称完整原目录全部内容未变。

原Thread和失败Turn已持久化并可重开；分页Replay每次扫描3条，到游标66，共42条公开事件，重开前后内容摘要及终态一致。
重开不是Turn取消、数据库备份恢复或Workspace效果撤销；本次没有这些操作，不补填通过结论。

## 5. 证据与源码追踪

公开结构化交付为[facts.json](facts.json)、[review-packet.json](review-packet.json)、[manifest.json](manifest.json)和本报告。
私有证据根角色记为E；个人路径、凭据、业务源码和保护状态不进入公开资料。

| E内证据 | 说明 |
|---|---|
| `beta-analysis-v1/network-v1/receipt.json` | 原实际运行、工具广告、前后输入、Attempt关联及重开收据 |
| `beta-analysis-v1/network-process-result.json` | 真实起止时间、宿主摘要、退出1与未自动重跑 |
| `beta-analysis-v1/network-delivery-v1/` | 完整Markdown报告、结构化事实、收据副本、费用快照、Review Packet及逐件SHA256清单 |
| `beta-analysis-v1/network-v1/state/` | 原Session保护持久状态；不复制到公开交付 |
| `beta-analysis-v1/offline-v2/` | 已有同宿主离线协议装配验证；模拟Provider结果不计真实模型请求或任务通过 |
| `beta-analysis-v1/network-diagnosis/` | 原认证历史只读诊断、四步原调用事实、原失败离线回放及工具能力反例；不新增模型请求 |

| 现有实现 | 关联符号与边界 |
|---|---|
| [agent_client.py](../../../src/harnessix/sdk/agent_client.py) | `AgentClient.initialize/create_thread/start_turn/replay_events`，真实协议请求与耐久游标 |
| [server.py](../../../src/harnessix/app_server/server.py) | `AgentProtocolServer`，握手及协议服务；不是私有宿主定义的新产品协议 |
| [agent_context.py](../../../src/harnessix/product_config/agent_context.py) | `build_product_agent_context`，共享产品指令；本次不修改Context策略 |
| [provider_verification_budget.py](../../../scripts/provider_verification_budget.py) | 既有费用Owner与Guard；真实账本并非产品账户计费硬限额 |
| [runtime.py](../../../src/harnessix/tools/runtime.py) | `CodingToolRuntime`，作用域内原生只读工具；不能推出任意Shell能力 |

以上源码和既有合同测试只是追踪关系，本记录不将合同测试结果充当真实业务验收。

## 6. Review Packet与剩余条件

结论为**HOLD**：认证协议及受Guard约束的真实Provider请求已经观察到，但源码分析未完成、业务整改未执行、业务测试未执行、人工任务验收未发生。
原工具事件及四步失败已由独立诊断确认；[通用导航整改](../../changes/m09-r3-bounded-source-navigation.md)保持原2751字节护栏、工具合同及预算。
离线回归不证明真实模型行为；后继候选与新请求另行冻结，原失败保留。
完整原目录快照、完整业务副本、取消观察、整改审批、最终测试、浏览器结果及使用者验收分别仍需完成。

R3质量门槛、Git正式写入、三平台消费安装、独立开发者Beta及正式1.0封板均未关闭，不能从一次协议连接、重开或费用结算推导商用完成。
