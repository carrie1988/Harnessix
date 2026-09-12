---
doc_type: governance
status: current
version: 30
code_revision: e1aa95764da726d2c1e8f286e4400579ce3efae7
owners:
  - core
modules:
  - documentation
related_adrs: []
related_tests: []
supersedes: []
---

# Harnessix Code 文档整改待办

## 1. 执行目标

本待办将[现状盘点](documentation-inventory.md)中的结构、状态和追踪缺口分解为可验收切片。执行顺序遵循“先建立导航与黄金样例，再按运行主链迁移，最后启用自动门禁”，避免一次性机械改写133份资料。

## 2. 完成边界

DOC-1整体完成必须同时满足：

1. 系统架构、源码阅读路线和文档角色具有唯一入口；
2. 30个顶层生产源码包都有与当前代码一致的模块设计；
3. 重大变更具备需求、架构、流程、时序、数据、失败、安全、测试和源码映射；
4. ADR、研究、现行设计、历史变更和验证证据职责清楚；
5. 新增或变更文档通过自动结构、状态、链接和追踪门禁；
6. 文档能指导未参与实现的工程师定位核心源码、验证行为和排查故障。

完成度不以新增文档数量、Mermaid数量或注释覆盖率单独判断。

## 3. 阶段总览

| 阶段 | 优先级 | 依赖 | 核心交付 | 状态 |
|---|---|---|---|---|
| DOC-1.0 | P0 | 0.9.0 | 规范、模板、全量盘点、追踪矩阵、机器基线、整改待办 | 已完成 |
| DOC-1.1 | P0 | DOC-1.0 | 文档总入口、系统架构、主链源码阅读路线 | 已完成 |
| DOC-1.2 | P0 | DOC-1.1 | Agent Runtime与Action Plane两份黄金样例 | 已完成 |
| DOC-1.3 | P0 | DOC-1.2 | Coding Agent核心运行链19个模块设计 | 已完成（19/19） |
| DOC-1.4 | P1 | DOC-1.2 | 产品运行时与扩展10个模块设计 | 进行中（8/10） |
| DOC-1.5 | P1 | DOC-1.3、DOC-1.4 | 聚合文档拆分、状态迁移、历史/证据治理 | 未开始 |
| DOC-1.6 | P0 | DOC-1.2最小规则；完整启用依赖DOC-1.5 | 自动文档门禁与防陈旧策略 | 未开始 |

DOC-1.1和DOC-1.2已经完成。0.9.1及后续重大产品实现必须在同一切片维护现行模块设计，不能重新累积“代码先行、资料追补”的债务。

## 4. DOC-1.0：盘点与规范

### 4.1 已交付

- [x] 固定提交上的133份Markdown全量目录和结构统计；
- [x] 30个顶层生产源码包及10个根级生产模块盘点；
- [x] 文档分类、单一事实源、状态、元数据和生命周期规范；
- [x] 重大变更判定与“设计—实现—测试—文档—关闭”工作流；
- [x] 图示、类、接口、数据、伪代码、源码和测试链接规范；
- [x] 详细设计、模块设计、重大变更、ADR和源码研究模板；
- [x] 文档—源码—测试目标追踪模型；
- [x] DOC-1.1～DOC-1.6待办和验收边界。

### 4.2 明确非目标

- 不重写存量模块设计；
- 不调整生产源码；
- 不把存量自由状态批量映射为新状态；
- 不引入CI检查脚本；
- 不宣称现有资料已符合新规范。

## 5. DOC-1.1：导航、系统架构与阅读总入口

状态：**已完成**。本阶段只建立系统级事实、边界和阅读入口；DOC-1.2现已完成`agent`模块，
其余29个包的独立现行模块设计由DOC-1.3～DOC-1.4交付。

### 5.1 交付项

1. [x] 新建`docs/README.md`，按“产品—架构—模块—契约—研究—决策—测试—部署—证据”导航；
2. [x] 按[详细设计模板](templates/detailed-design-template.md)重构[总体架构](../architecture.md)，明确当前/目标边界；
3. [x] 新建[源码阅读地图](../guides/source-reading-map.md)，覆盖CLI/App Server到Agent、Context、Model、Tool、Process、Workspace、Delivery、Action Plane的主链；
4. [x] 建立30个包和10个根级模块的所有权、依赖方向和禁止旁路规则；
5. [x] 建立正常任务、审批、取消、崩溃恢复和事务性交付五条系统级时序；
6. [x] 为系统级节点添加实际源码文件、关键符号和测试入口链接；
7. [x] 在文档中心标注根目录聚合文档角色，消除“当前实现”和“历史里程碑”混读。

### 5.2 验收

- [x] 新读者从`docs/README.md`不超过三次跳转可到达任一生产包当前源码地图或目标设计入口；
- [x] 总体架构中的每个已实现组件都有源码和测试映射；
- [x] 所有图均有文字说明，当前默认、显式装配和规划能力可明确区分；
- [x] 相对链接、Mermaid、格式和敏感信息检查通过。

## 6. DOC-1.2：两份黄金样例

状态：**已完成**。交付[Agent Runtime模块设计](../modules/agent.md)和
[Action Plane子系统设计](../subsystems/action-plane.md)，并完成源码反向核对、测试正向定位、链接、
元数据、敏感信息和Mermaid渲染验证。

### 6.1 Agent Runtime模块设计

目标文件：[Agent Runtime模块设计](../modules/agent.md)。

必须覆盖：

- `Kernel`/Runtime入口、Agent Loop、Reducer、Thread/Turn/Item/Event；
- Model、Context、Tool、Approval、Session之间的边界；
- 正常、多Tool、审批、Steering、取消、重试和恢复时序；
- 状态转换、不变量、持久事实顺序和Provider中立历史；
- 重点类/函数、字段、错误、观测、伪代码及测试映射；
- [agent源码包](../../src/harnessix/agent/)和[agent测试](../../tests/agent/)的推荐阅读路线。

### 6.2 Action Plane子系统设计

目标文件：[Action Plane子系统设计](../subsystems/action-plane.md)。

必须覆盖：

- Action Contract、Registry、Policy、Approval、Journal、Worker、Executor和Reconcile；
- `domain`、`policy`、`storage`、`executors`及根级`runtime.py`/`worker.py`职责；
- 请求指纹、幂等、租约、崩溃边界、`UNKNOWN`和无重复恢复；
- SQLite/PostgreSQL事务差异、审批安全和OpenTelemetry；
- 正常、拒绝、等待审批、租约丢失、宿主死亡和对账时序；
- 源码符号、合同测试、集成测试和故障注入映射。

### 6.3 黄金样例验收

- [x] 两份文档通过[文档工程规范](documentation-standard.md)全部适用项；
- [x] 由源码反向抽查至少10个关键符号，文档语义与实现一致；
- [x] 由文档正向定位至少5条测试，链接和测试目的准确；
- [x] 评审记录明确哪些章节和表格成为后续模块迁移范式；
- [x] 没有复制里程碑文档或用机械空图达标。

### 6.4 黄金样例评审记录

| 评审项 | Agent Runtime结果 | Action Plane结果 |
|---|---|---|
| 源码反向核对 | Runtime、Drive、Tool调度、Reducer、Session、取消、错误和恢复共12个以上关键符号 | Contract、Registry、Policy、Service、两个Journal、Worker和API共13组以上关键符号 |
| 测试正向定位 | 正常、多Tool、预算、取消、Retry、审批和崩溃恢复7类以上 | 正常、拒绝、审批、幂等、UNKNOWN、Lease、PostgreSQL和OTel 8类以上 |
| 当前/规划边界 | 默认只读产品链与可显式装配Patch/Process明确分离 | 当前本地/受信部署与尚未具备的公网身份边界明确分离 |
| 发现的实现风险 | 没有用文档掩盖已知产品装配和平台缺口 | 发现Action异常消息直接持久化`str(error)`，纳入0.9.4统一脱敏待办 |
| 后续迁移范式 | 摘要、边界、职责、状态、正常/失败/恢复、字段、接口、持久化、安全、观测、伪代码、源码测试映射、限制与变更记录 | 跨包子系统额外要求部署边界、事务后端差异、租约/UNKNOWN矩阵和独立模块去重策略 |

后续DOC-1.3/1.4必须复用上述**结构和评审口径**，不得复制两份文档的业务内容。Action Plane的
`domain`、`policy`、`executors`和`storage`独立模块设计已在DOC-1.3细化；它们引用子系统主链，
不维护第二份相互漂移的跨包状态机。

## 7. DOC-1.3：Coding Agent核心运行链

### 7.1 Wave A：模型、上下文和持久状态

进度：**4/4，已完成**。已完成[Session](../modules/session.md)、[Context](../modules/context.md)、[Artifact](../modules/artifacts.md)和[Model](../modules/models.md)模块设计。

| 顺序 | 源码包 | 目标文档 | 重点 |
|---:|---|---|---|
| 1 | `session` | [docs/modules/session.md](../modules/session.md) | Event Log、投影、22次迁移、CAS、恢复；已完成 |
| 2 | `context` | [docs/modules/context.md](../modules/context.md) | Source优先级、预算、检查、Compaction窗口；已完成 |
| 3 | `artifacts` | [docs/modules/artifacts.md](../modules/artifacts.md) | 大对象、覆盖证明、Diff和模型历史；已完成 |
| 4 | `models` | [docs/modules/models.md](../modules/models.md) | Provider端口、流式事件、尝试/用量/成本账本；已完成 |

### 7.2 Wave B：工具、补丁和进程执行

进度：**4/4，已完成**。[Coding Tool Runtime](../modules/tools.md)、
[Managed Patch Runtime](../modules/patches.md)、[Execution Plan](../modules/execution.md)和
[Process Runtime](../modules/processes.md)已完成。

| 顺序 | 源码包 | 目标文档 | 重点 |
|---:|---|---|---|
| 1 | `tools` | [docs/modules/tools.md](../modules/tools.md) | 只读/搜索/Git工具、分页、稳定模型视图；已完成 |
| 2 | `patches` | [docs/modules/patches.md](../modules/patches.md) | Prepared Patch、批次、Diff、Agent桥接和恢复；已完成 |
| 3 | `execution` | [docs/modules/execution.md](../modules/execution.md) | Execution Plan、持久存储和审批绑定；已完成 |
| 4 | `processes` | [docs/modules/processes.md](../modules/processes.md) | 跨平台进程、PTY、Owner、租约和终态；已完成 |

### 7.3 Wave C：Action、安全与可信边界

进度：**7/7，已完成**。[Domain](../modules/domain.md)、[Policy](../modules/policy.md)、
[Executors](../modules/executors.md)、[Storage](../modules/storage.md)、
[Sandbox](../modules/sandbox.md)、[Secrets](../modules/secrets.md)和
[Trusted Actions](../modules/trusted-actions.md)已完成。Wave D当前进度为**4/4，已完成**，包括
[Workspace](../modules/workspace.md)、[Delivery](../modules/delivery.md)、
[Evals](../modules/evals.md)和[Observability](../modules/observability.md)。

| 顺序 | 源码包 | 目标文档 | 重点 |
|---:|---|---|---|
| 1 | `domain` | [docs/modules/domain.md](../modules/domain.md) | Action领域对象、状态和错误；已完成 |
| 2 | `policy` | [docs/modules/policy.md](../modules/policy.md) | 决策输入、风险、审批策略；已完成 |
| 3 | `executors` | [docs/modules/executors.md](../modules/executors.md) | Executor合同、副作用和对账；已完成 |
| 4 | `storage` | [docs/modules/storage.md](../modules/storage.md) | Action Journal、Schema/Migration、队列、租约、多Worker Claim和恢复；已完成 |
| 5 | `sandbox` | [docs/modules/sandbox.md](../modules/sandbox.md) | Profile、Container、网络和能力探测；已完成 |
| 6 | `secrets` | [docs/modules/secrets.md](../modules/secrets.md) | Secret引用、注入、守卫和脱敏；已完成 |
| 7 | `trusted_actions` | [docs/modules/trusted-actions.md](../modules/trusted-actions.md) | 统一风险路由、审计链和扩展边界；已完成 |

### 7.4 Wave D：工作区、交付、评测与观测

进度：**4/4，已完成**。[Workspace](../modules/workspace.md)、[Delivery](../modules/delivery.md)、
[Evals](../modules/evals.md)和[Observability](../modules/observability.md)均已完成。

| 顺序 | 源码包 | 目标文档 | 重点 |
|---:|---|---|---|
| 1 | `workspace` | [docs/modules/workspace.md](../modules/workspace.md) | 路径、选择资源Snapshot、Secure Reader、Fencing Lease和一致性；已完成 |
| 2 | `delivery` | [docs/modules/delivery.md](../modules/delivery.md) | Workspace Transaction、私有Blob、Rollback、Git Worktree/Checkpoint/Commit和Push；已完成 |
| 3 | `evals` | [docs/modules/evals.md](../modules/evals.md) | 任务合同、分级、Campaign、预算和交付证据；已完成 |
| 4 | `observability` | [docs/modules/observability.md](../modules/observability.md) | Trace/Metric/Log、持久传播、低基数、异常隐私和分链故障隔离；已完成 |

### 7.5 每个模块的完成条件

- 按[模块设计模板](templates/module-design-template.md)完整编写；
- 至少包含正常和失败/恢复时序；有持久状态或敏感数据时补状态/数据流图；
- 所有公共端口、关键状态和高风险副作用有源码符号及测试映射；
- 从现有聚合文档抽取当前事实，不删除历史证据；
- 对发现的文档与源码不一致建立缺陷，不以修改文档掩盖实现问题。

## 8. DOC-1.4：产品运行时与扩展

状态：**进行中（8/10）**。Protocol、App Server、SDK、Product Config、API、Adapter、MCP与Skill现行模块设计已完成，下一项为Hook。

| Wave | 源码包 | 目标文档 | 重点 |
|---|---|---|---|
| 产品协议 | `protocol` | [Protocol模块设计](../modules/protocol.md) | 已完成：JSON-RPC、Schema、Replay、幂等、投影白名单及真实协商差距 |
| 产品协议 | `app_server` | [App Server模块设计](../modules/app-server.md) | 已完成：连接状态、应用命令、后台Turn、Replay/Delta、stdio背压关闭、Scoped Artifact与默认装配边界 |
| 产品协议 | `sdk` | [SDK模块设计](../modules/sdk.md) | 已完成：双客户端、进程内/子进程、并发取消、身份/游标恢复、HTTP资源及真实加固差距 |
| 产品装配 | `product_config` | [Product Config模块设计](../modules/product-config.md) | 已完成：严格配置、迁移、Profile CAS、Secret引用、安全Fallback和启动事务 |
| 产品装配 | `api` | [API模块设计](../modules/api.md) | 已完成：Action资源、Lifespan、状态码、错误、Trace、身份、资源预算和部署边界 |
| 产品装配 | `adapters` | [Adapter模块设计](../modules/adapters.md) | 已完成：LangChain Tool映射、固定Context、状态投影、Tool Call身份、幂等恢复与真实LangGraph证据边界 |
| 扩展协议 | `mcp` | [MCP模块设计](../modules/mcp.md) | 已完成：受管Target、目录/Schema/结果合同、SQLite状态、调用前漂移、Trusted Action、UNKNOWN/Reconcile、只读stdio Server和生产差距 |
| 扩展 | `skills` | [Skill模块设计](../modules/skills.md) | 已完成：本地来源、Frontmatter、目录摘要、冲突消歧、渐进加载、安全Reader、访问账本、Action Gateway、Secret/提示注入和生产差距 |
| 扩展 | `hooks` | `docs/modules/hooks.md` | 声明式注册、摘要授权、超时和恢复 |
| 验证 | `smoke` | `docs/modules/smoke.md` | 请求预算、白名单、脱敏和停止条件 |

验收标准与DOC-1.3一致，并额外覆盖Protocol兼容、扩展供应链、客户端断连和Secret零暴露。

## 9. DOC-1.5：聚合、历史和证据治理

### 9.1 聚合文档处理

| 文档 | 处理策略 | 禁止事项 |
|---|---|---|
| [测试与Eval](../testing-and-evals.md) | 保留总规范；拆出合同、任务集、Campaign、成本和证据索引 | 不复制各模块测试清单 |
| [部署与运行](../deployment.md) | 拆分安装、配置、升级、恢复、诊断和平台差异 | 不把命令输出当设计 |
| [0.5 Coding Tools](../m05-coding-tools.md) | 转为里程碑历史索引，当前事实链接模块设计 | 不继续追加跨模块细节 |
| [0.8产品运行时](../m08-product-runtime-and-extensions.md) | 转为里程碑历史索引，当前事实链接10个模块设计 | 不与现行模块设计双写 |
| [威胁模型](../threat-model.md) | 保留统一威胁登记，具体缓解链接模块设计和测试 | 不在多个文档维护不同风险状态 |
| [总体架构](../architecture.md) | 保留系统级边界和主链，不承载每个类字段 | 不扩展为源码百科 |
| [路线图](../roadmap.md) | 只保留范围、依赖、状态和证据入口 | 不堆积实现日志 |

### 9.2 状态与生命周期迁移

1. 为当前事实源添加标准YAML头；
2. 将里程碑增量设计标记为`historical`，并链接现行模块设计；
3. ADR保持决策原文，新增标准状态和被取代关系；
4. 研究资料固定参考版本和访问日期；
5. 验证证据固定代码提交、环境、预算和脱敏规则；
6. 不确定是否仍有效的资料先标记`reviewing`，经源码核验后再归类。

## 10. DOC-1.6：自动化门禁

### 10.1 首批阻断规则

- 新增正式文档必须有合法YAML元数据和状态枚举；
- 本次修改的已迁移文档必须具有要求章节；
- 相对路径、标题锚点、源码和测试目标必须存在；
- 不允许本机绝对路径、疑似凭据和已禁止的过程性措辞；
- 重大变更必须存在变更设计并更新受影响模块设计；
- 模块设计必须至少包含一个源码文件链接和一个测试文件链接；
- `code_revision`必须为`pending`或可解析的40位提交，并符合文档生命周期；
- Mermaid代码块执行语法检查。

### 10.2 渐进启用

1. 先对`docs/governance/`、`docs/modules/`和`docs/changes/`严格阻断；
2. 存量目录只报告，不因历史债务阻断无关提交；
3. 每迁移一类文档，将其加入严格范围并更新机器基线；
4. 最终建立源码包变更与模块文档同步检查，允许通过明确的“行为不变”证据豁免；
5. 门禁实现必须有正反测试，避免靠关键词和空章节达标。

## 11. 风险与控制

| 风险 | 表现 | 控制 |
|---|---|---|
| 机械补文档 | 大量空章节、重复源码、无语义图 | 黄金样例评审后再批量迁移；人工抽查关键符号 |
| 文档双写 | 聚合文档与模块设计描述冲突 | 明确单一事实源；历史文档只链接当前设计 |
| 链接快速漂移 | 重构后行号、路径失效 | 现行文档使用文件链接+符号；历史文档使用永久链接 |
| 过早严格CI | 存量债务阻断所有开发 | 按已迁移目录渐进启用 |
| 文档挤占产品迭代 | 长期只整理资料 | 每个阶段可独立验收；DOC-1.2后与0.9切片同步维护 |
| 泄露环境或Secret | 验证资料包含个人路径、服务器或凭据 | 门禁扫描、示例脱敏、验证证据评审 |
| 实现缺陷被文档掩盖 | 为匹配代码而合理化错误行为 | 发现契约/安全/恢复问题单独建缺陷并测试闭环 |

## 12. 进度报告口径

每次DOC-1阶段报告只使用以下量化口径：

- 已迁移/目标模块数；
- 具有源码与测试链接的现行设计数；
- 已标准化状态的文档数；
- 链接、结构和Mermaid门禁结果；
- 发现并关闭的文档—源码不一致数量；
- 尚未覆盖的失败、安全、迁移或平台风险。

不得以总字数、图数量或文档数量替代可读性和可追溯性结论。
