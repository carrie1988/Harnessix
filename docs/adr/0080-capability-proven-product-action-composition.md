---
doc_type: adr
status: current
version: 3
code_revision: 328aa2d6c8ee85a75ab2baef51b80869dc4089a8
owners:
  - core
modules:
  - agent
  - trusted_actions
  - artifacts
  - patches
  - processes
  - delivery
  - product_config
related_adrs:
  - docs/adr/0065-platform-capability-ports-and-execution-plan.md
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0070-agent-protocol-v1-boundaries.md
  - docs/adr/0079-preflight-and-native-read-port.md
related_tests:
  - tests/trusted_actions/test_router.py
  - tests/trusted_actions/test_agent_gateway.py
  - tests/agent/test_runtime.py
  - tests/agent/test_trusted_action_runtime.py
  - tests/agent/test_schemas.py
  - tests/agent/test_session_upgrade.py
  - tests/protocol/test_projection.py
  - tests/delivery/test_filesystem.py
  - tests/integration/test_container_sandbox.py
  - tests/product_config/test_server_and_cli.py
supersedes: []
---

# ADR 0080：能力证明驱动的默认Trusted Action组合根

## 状态

接受，0.9.1e按本决策实施。0.9.1e1目录地基已经通过全矩阵验收，0.9.1e2 Agent Gateway、Agent Event v20与双账本恢复已经完成本地实现、专项回归和全仓门禁，等待远端CI验收；默认Patch/Process产品能力仍未完成。实现状态由
[0.9.1e详细设计](../changes/m09-1e-default-trusted-action-composition.md)和现行模块文档维护。

## 背景

Harnessix已经具备Artifact、单/多文件Patch、Process、Execution Plan v2、Workspace Transaction、Sandbox、Policy、Approval、
Effect Journal与Reconcile能力，但默认[`run_product_stdio`](../../src/harnessix/product_config/server.py)只装配只读
`CodingToolRuntime`。代码库能力和真实产品能力因此分离：模型看不到受控写入/执行，用户无法在默认TUI审批完整Diff，进程重启
也不会由Agent统一驱动Trusted Action恢复。

现有Patch、Patch Batch和Process Bridge分别早于统一Trusted Action Router形成，各自保存专用计划、审批与恢复事实。若直接
并列接入产品，会产生以下风险：

1. Tool目录与可执行注册表分别维护，可能广告没有安全Executor的能力；
2. Agent审批与Execution Approval Checkpoint成为两套权威；
3. 同一个副作用可能同时存在专用Bridge账本和Trusted Action Hash链，恢复结论冲突；
4. Windows普通目录写入或宿主Shell可能在缺少等价安全端口时被弱化开放；
5. 每新增高风险Tool都扩张`AgentRuntime`专用分支，难以长期维护和测试。

[专项源码研究](../research/default-trusted-action-product-composition.md)证明成熟Coding Agent普遍让模型可见目录与实际执行注册
相关，并把审批/Sandbox放在执行前的集中边界。Harnessix应在此基础上发挥已有持久计划、资源快照、Hash链、Fencing和
`UNKNOWN -> Reconcile`能力，而不是复制参考项目内部结构。

## 决策驱动因素

1. 模型只能看到当前宿主确实能够安全执行的能力；
2. Tool Descriptor、输入Schema、Effect、Risk、Recovery与Executor必须来自同一可信绑定；
3. 用户决定必须精确绑定批准时展示的Plan和Diff，不能只绑定Tool名；
4. Agent Session和Trusted Action Router不能分别拥有副作用批准权威；
5. 崩溃或响应丢失后不得盲重放非幂等写入；
6. Artifact、完整Diff、进程输出必须有Thread/Turn/Call作用域和有界模型视图；
7. Process只能使用已验证的强Sandbox与固定Profile，不能把模型输入当作宿主Shell；
8. Windows缺少等价写入端口时必须不广告，不能退回字符串Path或不受管子进程；
9. Agent Protocol v1既有客户端必须继续解析公共审批，不因内部统一路由被强制升级；
10. Product Config v2摘要与现有配置必须保持兼容，执行Profile不能静默塞入v2；
11. 每个阶段必须可独立回滚，且回滚不能破坏已有Session与只读产品链。

## 候选方案

| 方案 | 满足的驱动因素 | 不满足的驱动因素 | 成本/风险 |
|---|---|---|---|
| A. 直接把现有Patch/Batch/Process Bridge并列装入Server | 能快速出现更多Tool；复用现有测试 | 多审批权威、目录与执行不同源、Delivery未真正发布到Workspace | 恢复Saga继续膨胀，无法证明无旁路 |
| B. 每个Tool直接调用`TrustedActionRouter`，Agent仍使用旧审批 | 执行经过Router | Agent决策与Router决策可能漂移；每个Tool重复适配 | 双写和崩溃窗口复杂 |
| C. 新增通用Agent Trusted Action Gateway，目录与Executor由能力证明共同生成 | 满足全部安全与恢复驱动因素；保留现有Router | 需要内部事件Schema升级、产品组合和专项E2E | 初期改动较多，但形成长期扩展边界 |
| D. 所有平台固定广告，调用时探测 | Tool目录稳定 | 违反真实能力原则；模型反复选择不可用Tool | 用户体验差且可能触发弱降级 |
| E. 把执行Profile加入Product Config v2可选字段 | 单文件配置 | 改变v2 Schema与摘要，旧配置兼容语义不清 | 需要正式v3迁移，不适合0.9.1e |

## 决策

选择方案C，并采用独立版本化`ProductActionConfigV1`承载可选受控Process Profile。必须遵守以下不可违反的不变量。

### 1. 目录与执行同源

产品启动时创建一个`ProductActionCatalog`。每个候选能力先完成平台、依赖、Sandbox、Executor和恢复能力探测；只有验证成功的
候选才能同时：

- 注册到`TrustedActionRouter`；
- 转换为模型可见`ToolDescriptor`；
- 进入稳定能力报告。

必须断言“已广告Binding集合 = 可执行注册Binding集合”。没有Executor、Reconcile或强Sandbox证据的高风险Tool不得仅凭
Schema进入目录。

### 2. Router是唯一执行批准权威

Agent增加通用`TrustedActionGateway`端口。Agent Session中的审批Item只负责暂停/恢复交互和公共投影，必须保存Router
`plan_id`、Plan Fingerprint、Policy ID/Version、呈现类型和可选Diff Artifact。用户响应经过Agent归属检查后，必须先由Router
原子记录Execution Approval Checkpoint，再把同一决策持久化到Session。

若两个账本在崩溃窗口不一致，恢复以Router计划状态和精确决策内容为执行权威；Agent只能补齐相同决策或投影稳定冲突，不能
覆盖Router终态。

### 3. 内部事件升级，公共协议保持兼容

内部Agent Event新增专用`trusted_action_approval_request`和通用Action Effect，提升事件Schema版本并保留旧事件读取。公共
Agent Protocol v1继续投影为已有`approval_type`枚举：Patch为`patch_batch`，Process为`process`，其他受控Action为`tool`。
公共字段仍是`approval_id/call_id/request_fingerprint/policy_version/decision/diff_artifact`，不向旧客户端暴露完整Execution Plan。

0.9.1e不得为方便实现而扩展公共v1枚举；未来需要新公共字段时必须通过协议版本或协商能力发布。

### 4. Patch通过Delivery事务真实发布

默认写工具采用现有多文件Patch合同。Gateway从模型调用构造确定性Invocation ID；Router冻结资源Snapshot与Policy；Review
Provider从同一Plan生成完整Diff并事务发布Artifact。只有批准后，Executor才通过`WorkspaceTransactionRuntime`与Workspace
Fencing Lease把精确事务发布到用户Workspace。

Executor不能调用旧`ManagedPatchBridge`把“受管副本已写”冒充真实交付。事务ID、Blob摘要、源Snapshot、目标状态和Plan ID必须
相互绑定；部分效果只通过Delivery Ledger对账。

POSIX普通目录写入可在安全端口验证后广告。Windows在抗Reparse事务Writer完成前不广告该Tool。受管Git Worktree可以作为
后续Windows写入实现，但不能在本决策中用候选状态替代真实验证。

### 5. Process只允许固定Profile与强Sandbox

Process的公共输入只选择宿主持有的Profile及有界参数，不接受任意程序、Shell字符串、环境或Secret值。独立
`ProductActionConfigV1`必须固定：Profile ID、版本、命令模板、允许参数、不可变容器镜像Digest、网络模式、资源/输出/超时上限和
Secret引用集合。

启动时必须重新探测容器引擎、镜像身份、网络能力和Owner清理语义。探测失败时不广告Process。Host Process Runtime只保留为
库级能力，不能作为产品强Sandbox的自动Fallback。

### 6. Artifact默认装配并按作用域读取

产品Session数据库旁默认创建`SQLiteArtifactStore`。Tool完整输出、Patch审查Diff和Process输出都必须使用已有Thread/Turn/Call
作用域；Protocol只通过`ScopedProtocolArtifactReader`读取。新增Artifact purpose必须进入严格合同和事务测试，不能用任意字符串
绕过作用域。

### 7. UNKNOWN只对账，不重放

产品启动和Agent恢复先调用`recover_interrupted`把遗留`running`转换为`unknown`，再对每个待结算Plan调用`reconcile`。Patch
依据Workspace Transaction Ledger，Process依据受管Process/Container Ledger。`unknown`或`manual_intervention`必须形成稳定Tool
Result和可操作诊断；任何路径都不得再次调用`execute`。

### 8. 功能门回滚

高风险目录由默认关闭的版本化功能门逐项开启，直到专项E2E和三平台矩阵通过。关闭功能门必须只停止新Plan进入目录，不能删除
旧Plan、审批、Artifact或效果账本；旧在途Plan仍由恢复Owner结算。

## 理由

方案C把已有统一路由从“库级能力”提升为产品权威，同时让Agent只关心一个通用高风险暂停/恢复端口。它避免复制Patch、Process、
Git Push等每类工具的审批状态机，也使能力目录可以从同一Binding生成，不再由Prompt、UI和Executor各自维护。

独立Action配置优于直接扩展Product Config v2：v2当前只描述Provider/Profile/Secret与Fallback，已经具有严格Schema和摘要。把容器
命令与执行权限加入可选字段会改变其职责和摘要，形成难以解释的兼容变更；独立v1可单独CAS、诊断和升级，未来再决定是否由
Product Config v3引用。

公共协议保持现有审批形状，是因为旧客户端已经严格解析`approval_type`。内部新增专用事件能够持久化Plan绑定，而公共端只需要
安全交互摘要和Artifact引用；无需为了内部统一而破坏协议v1。

## 后果

### 正面后果

- 默认产品首次能够真实执行可恢复的多文件写入和受控进程；
- 模型目录与实际可执行能力不会漂移；
- 用户批准的Diff与最终执行Plan有可验证绑定；
- 所有新高风险Tool共享Policy、Approval、Journal、Sandbox和Reconcile；
- Windows及缺少容器能力的宿主会诚实省略未证明Tool；
- 旧Agent Protocol v1客户端继续工作；
- Artifact成为产品默认能力，而非示例或显式库装配。

### 负面后果与债务

- Agent内部Event Schema需要升级并增加旧事件兼容测试；
- Session与Router两个SQLite事实源仍存在决策传播Saga，必须通过确定性ID、查询优先和恢复测试约束；
- POSIX普通目录写与Windows写能力暂时不对称；
- Process需要额外配置与容器依赖，首次使用成本增加；
- Product Server组合根会增加资源Owner，必须拆出Builder/Owner而不是继续放大单函数；
- Router通用异常清洗、Owner锁和遥测仍需0.9.3/0.9.4继续加固。

## 兼容、安全与运维影响

- Product Config v2及其摘要不变；新增Action Config v1是独立可选输入；
- Agent Event仅追加新Schema版本，旧Session必须继续读取；旧专用Patch/Process事件不迁移、不改写；
- Agent Protocol v1 JSON Schema保持已有枚举和字段，SDK无需强制升级；
- 默认创建Artifact、Execution Plan、Trusted Action Audit、Delivery与Process状态表，必须继续使用同一私有State Root；
- 回滚版本不能执行它不认识的新在途Plan，因此发布前必须先排空或使用兼容恢复命令；数据文件不得自动删除；
- 日志与指标只记录摘要、状态和低基数能力ID，不记录Patch正文、argv、绝对路径、环境值或Secret；
- 功能门关闭、能力探测失败和平台省略必须出现在Doctor/Preflight可操作报告中。

## 验证方式

1. Catalog属性测试证明广告Binding和Router注册Binding完全相等；
2. 无容器、镜像漂移、Windows普通目录写、未知平台和错误配置均不广告且稳定诊断；
3. Agent内部事件新旧Schema、Session恢复和公共Protocol v1兼容测试；
4. Patch正常链：模型调用→Plan→完整Diff Artifact→审批→事务提交→Git Status/Read验证；
5. Patch在计划后漂移、审批后漂移、租约丢失、部分效果、提交返回丢失和重启恢复测试；
6. Process正常、非零退出、超时、取消、Owner丢失、容器引擎重启、输出截断和Artifact测试；
7. 重复审批、重复execute、Protocol请求重放和跨Thread/Turn/Call Artifact越权测试；
8. TUI与SDK观察同一审批、Diff、进度、终态和恢复结果；
9. POSIX真实文件系统与固定镜像Container集成测试；Windows验证不广告高风险能力且只读链不回退；
10. 全量Ruff、Mypy、Schema、文档、Readability、Linux Python 3.12/3.13、macOS、Windows、PostgreSQL和Container CI。

## 关联资料

| 类型 | 路径 | 关系 |
|---|---|---|
| 源码研究 | [默认Trusted Action产品组合源码研究](../research/default-trusted-action-product-composition.md) | 固定参考与现状证据 |
| 重大变更设计 | [0.9.1e详细设计](../changes/m09-1e-default-trusted-action-composition.md) | 实施合同、流程与切片 |
| 现行模块 | [Agent](../modules/agent.md)、[Trusted Actions](../modules/trusted-actions.md)、[Artifacts](../modules/artifacts.md)、[Patches](../modules/patches.md)、[Processes](../modules/processes.md)、[Delivery](../modules/delivery.md)、[Product Config](../modules/product-config.md) | 实现完成后的当前事实源 |
| 协议 | [Agent Protocol](../modules/protocol.md)、[App Server](../modules/app-server.md) | 公共兼容边界 |
| 安全 | [威胁模型](../threat-model.md)、[Sandbox](../modules/sandbox.md) | 权限、平台和隔离边界 |
| 测试 | [`tests/trusted_actions`](../../tests/trusted_actions/)、[`tests/agent`](../../tests/agent/)、[`tests/delivery`](../../tests/delivery/)、[`tests/processes`](../../tests/processes/)、[`tests/product_config`](../../tests/product_config/) | 合同、故障和产品纵向验证入口 |

## 被取代关系

本ADR不取代ADR 0065～0069，而是规定这些库级可信执行合同如何进入默认产品。旧Patch/Process专用Bridge仍保留用于读取历史
Session和库级兼容，但新增产品高风险能力不得继续复制其专用审批权威。未来若统一事务数据库、升级公共协议或开放远端执行，
必须由新ADR明确取代相应条款。
