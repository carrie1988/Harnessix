---
doc_type: adr
status: current
version: 1
code_revision: 0bc942bce8aeb22747a06515732936d1a312cd02
owners:
  - core
modules:
  - trusted_actions
  - product_config
  - processes
  - artifacts
  - session
related_adrs:
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0089-bounded-local-transport-lifecycle.md
  - docs/adr/0090-plan-first-store-maintenance-and-backup.md
related_tests:
  - tests/trusted_actions/test_router.py
  - tests/trusted_actions/test_agent_gateway.py
  - tests/product_config/test_action_recovery.py
  - tests/product_config/test_server_and_cli.py
supersedes: []
---

# ADR-0091：Action Runtime采用双层Owner栅栏、持久Operation期限与只对账恢复

## 状态

接受并由0.9.3c实现。代码Revision为`0bc942bce8aeb22747a06515732936d1a312cd02`，本地全仓门禁为
`3595 passed, 32 skipped`。[首次六实例CI 35499848035](https://github.com/carrie1988/Harnessix/actions/runs/35499848035)
的Documentation因缺少同批设计资料、Container因Session未初始化而失败；修复版补齐文档并在Action组合根内幂等初始化Session。
修复版六实例CI通过前不得标记0.9.3c正式关闭。

本ADR进一步落实[ADR 0081](0081-single-coding-agent-product-boundary.md)：Action能力是唯一Coding Agent进程内的执行内核，
不是可独立部署的Action Plane产品。它不恢复HTTP API、Worker队列、独立认证面或第二套外部调用入口。

## 背景

Harnessix Code已经把模型Tool Call、MCP、Skill、Hook和内建Coding能力收敛到`TrustedActionRouter`。Router在调用Executor前
持久化`running`或`reconciling`，并用UNKNOWN表达无法证明的外部效果。但0.9.3c之前仍存在以下生产风险：

1. 两个产品宿主可同时打开Action Audit、Execution Plan和Process Owner，旧宿主没有持久Generation栅栏；
2. `running/reconciling`只有Route状态，没有“谁、何时、哪一轮”执行的持久Operation事实；
3. Executor无Route级Deadline，超时、Task取消与进程自身Deadline没有统一结算规则；
4. Executor已产生外部效果而最终Audit提交失败时，重启只能看到`running`，但缺少明确的中断Operation证据；
5. Execution Plan、Action Audit、Session、Artifact和Process Lease分属多个Store，崩溃窗口会留下孤儿或单边事实；
6. 若恢复代码直接重新调用Execute，非幂等写操作可能重复产生真实外部效果；
7. 独立HTTP/Worker虽然能提供另一个Owner，但会重新扩大产品拓扑、权限和运维面，不符合当前Coding Agent定位。

详细源码研究与实现流程见[可靠性、背压与长期运行研究](../research/reliability-and-performance.md)和
[0.9.3c详细设计](../changes/m09-3c-action-runtime-fencing-and-recovery.md)。

## 决策驱动因素

1. **零重复效果优先**：恢复率低可以进入人工处置，非幂等写不能因超时或重启自动重放；
2. **唯一产品入口**：CLI/TUI/SDK仍只进入Agent Protocol和Agent Runtime，不增加Action独立API；
3. **失效宿主不可提交**：文件锁只证明当前占用，持久Generation才能拒绝迟到的旧Owner；
4. **崩溃可解释**：每次Execute/Reconcile必须有Operation ID、轮次、Owner、Deadline和终结状态；
5. **事务边界真实**：Operation与Action Route在同一SQLite事务提交，但跨Store扫描不得伪装分布式事务；
6. **取消不等于撤销**：Python Task取消或调用方断线不证明外部效果没有发生；
7. **诊断低敏**：Owner原始Token、Action参数、路径、输出正文和异常正文不得进入公开报告；
8. **前向兼容**：Action Audit Schema v1必须无损升级为v2；
9. **平台一致**：macOS、Linux和Windows共享合同，文件锁实现复用现有跨平台原语；
10. **源码可维护**：Router保持薄编排，状态、Owner和Operation持久化按职责拆分。

## 候选方案

| 方案 | 优点 | 主要问题 | 结论 |
|---|---|---|---|
| 恢复独立Action HTTP/Worker | 可单独扩缩和排队 | 重新引入第二产品入口、认证、部署和状态同步，且本地Coding Agent无此必要 | 拒绝 |
| 仅依赖SQLite单写者 | 实现最少 | 不阻止两个宿主交替提交，无法识别旧Owner迟到写 | 拒绝 |
| 仅使用进程文件锁 | 可排除当前竞争 | 不能在锁转移后拒绝旧内存对象或旧Operation提交 | 拒绝 |
| 只保存Route状态 | 数据少 | 无Deadline、尝试次数、Owner绑定和中断证据 | 拒绝 |
| 超时后把写操作标记failed并重试 | 用户表面成功率高 | 无法证明外部写未发生，可能重复Push、发布或命令效果 | 拒绝 |
| 重启后自动重新Execute | 恢复简单 | 非幂等效果重复，违反效果安全不变量 | 拒绝 |
| 双层Owner + Operation账本 + UNKNOWN/Reconcile | 失败语义可审计，旧Owner失效，恢复不重放 | 增加Schema、扫描和保守人工处置 | 采用 |
| 为跨Store引入分布式事务协调器 | 可追求原子提交 | 本地SQLite拓扑复杂化，外部效果仍无法参与原子事务 | 拒绝 |
| 启动时只读扫描并保守修复可证明缺口 | 与实际Store边界一致，失败关闭 | 可能报告不能自动清理的孤儿 | 采用 |

## 决策

### 1. Action Runtime仍是Coding Agent内部能力

产品主链保持：

```text
CLI/TUI/SDK -> Agent Protocol -> Agent Runtime -> TrustedActionRouter -> Executor
```

`serve`、`agent`、默认产品进程拥有Action Runtime生命周期。不会重新加入`action serve`、`worker`、Action HTTP客户端或独立
Action数据库部署。外部框架若需要复用，应通过Agent Protocol、SDK或未来经过正式评审的薄适配进入同一Runtime，而不是绕过
Session、Policy和Approval。

### 2. 使用两层Owner而不是一个“万能锁”

第一层是[`product_action_runtime_lock`](../../src/harnessix/product_config/action_owner.py)，在打开Execution Plan Store、Action Audit
Store和Process Supervisor之前取得`product-action-runtime.lock`。它排除同一State Root的第二产品宿主，避免多个Store分别成功打开后
才发现竞争。

第二层是[`SQLiteActionAuditStore.runtime_owner`](../../src/harnessix/trusted_actions/ownership_store.py)。每次取得
`action-audit.db.runtime.lock`后，在数据库中原子递增`owner_generation`并替换`owner_token_sha256`。所有产品Action写入必须调用
`_assert_runtime_owner`校验内存Fence与持久Generation/Token摘要。

两层Owner职责不同：产品锁保护组合根启动顺序；Audit Fence保护每次Action写提交。不能删除任一层后仍宣称语义等价。

### 3. 原始Owner和Operation Token只驻留内存

`ActionRuntimeFence.token`和`ClaimedActionOperation.token`是当前宿主能力，不写入数据库、日志、Schema或恢复报告。持久层只保存
SHA-256摘要。Token不是跨用户认证方案，而是防止错误对象或旧Operation在同一信任域内提交；同UID恶意进程仍属于0.9.4威胁
模型与OS隔离范围。

### 4. 每次Execute/Reconcile先原子Claim Operation

Action Audit Schema v2新增`action_route_operations`。Claim在同一个`BEGIN IMMEDIATE`事务中完成：

1. 校验Runtime Fence；
2. 计算`phase`的下一`attempt`并检查上限；
3. 将Route从`ready -> running`或`unknown -> reconciling`；
4. 写入Operation ID、Plan ID、Phase、Attempt、Owner Generation、Token摘要、开始时间和Deadline；
5. 提交后才调用Executor。

若事务失败，Executor不会被调用。若Executor完成而最终提交失败，Route与Operation仍保持执行中；新Owner只能把它中断为UNKNOWN，
不能再次Execute。

### 5. Operation完成与Route终态同事务提交

`complete_operation`先验证：

- 当前Runtime Fence仍有效；
- Operation的Owner Generation与当前Fence一致；
- 数据库Operation仍为`active`；
- 调用方持有Token的摘要与Operation一致；
- Route仍处于该Phase对应的执行态。

随后在一个事务内追加Action Audit Event、CAS更新Route快照，并将Operation改为`completed`。任何一步失败全部回滚。

### 6. Deadline是持久证据与当前宿主计时器的组合

Operation保存有时区UTC Deadline，供重启扫描判断过期；当前宿主使用`asyncio.timeout`执行协作取消。Process Profile已有自身强制
超时与进程树清理，因此产品Route执行期限取全部固定Profile最大超时加30秒清理余量，且不低于300秒。

Route Deadline不承诺强制终止任意屏蔽取消的第三方Python协程。受信Executor必须遵守取消合同；不可信代码继续由Process
Supervisor/Container的强Owner和资源边界执行。

### 7. 写效果超时或取消只能进入UNKNOWN

| 场景 | READ_ONLY | IDEMPOTENT/NON_IDEMPOTENT_WRITE |
|---|---|---|
| Execute超时 | `failed/executor_timeout` | `unknown/write_effect_timeout_unknown` |
| Execute Task取消 | `failed/executor_cancelled`后向上重抛取消 | `unknown/cancelled_write_effect_unknown`后向上重抛取消 |
| 未知异常 | `failed/executor_error` | `unknown/unexpected_write_error` |
| `UncertainEffectError` | `unknown` | `unknown` |
| Reconcile超时/取消/异常 | `unknown` | `unknown` |

取消向上层传播前必须先尽力提交保守结果。UNKNOWN只能调用Reconcile，不能回调Execute。

### 8. Reconcile有界且耗尽后人工处置

每个Plan默认最多3次Reconcile Operation，合法配置范围1～128。每次都有独立Deadline和Attempt。达到上限时Route从UNKNOWN进入
`manual_intervention/reconciliation_attempts_exhausted`，不再调用Executor。`recovery_mode=none`直接进入人工处置。

### 9. 新Owner中断遗留Operation，不重放效果

启动恢复遇到`running/reconciling`时，`interrupt_operation`在一个事务中把最新Active Operation标记`interrupted`，同时把Route
迁移为UNKNOWN。之后由既有Agent/Gateway恢复逻辑调用Reconcile。此路径禁止调用Execute。

### 10. 跨Store采用保守扫描，不伪造全局原子性

[`scan_product_action_recovery`](../../src/harnessix/product_config/action_recovery.py)在Owner窗口内扫描：

- Action Route与Execution Plan：缺失Plan可从不可变Route内嵌Plan修复；内容不一致失败关闭；
- Session引用：引用不存在Route时失败关闭；产品Route缺少Session引用只计数报告，不自动删除；
- Action Review/Output Artifact：无Session Call引用只计数报告，不读取或删除正文；
- Process Lease：活跃Lease无匹配非终态Route时调用既有Reconcile并把孤儿计数为启动失败；
- Operation：报告Active和已过期数量，不据Deadline直接伪造外部终态。

扫描结果是低敏、摘要绑定的`ActionRecoveryScanReport`，先于原有启动恢复报告持久化到Product Config Store。

## 数据与Migration决策

Action Audit Schema从v1升级为v2：

- 保留所有`action_route_plans`、`action_route_snapshots`和`action_audit_events`；
- 新增Owner元数据，缺省Generation为0、Token摘要为64个`0`；
- 新增Operation表和Active索引；
- 升级完成后才把`schema_version`改为2；
- 未知版本失败关闭；不提供自动降级。

Product Config Store新增`product_action_recovery_scans`，以报告SHA-256为主键保存规范JSON。相同摘要不同正文视为损坏。

## 安全与隐私

1. 文件锁使用`O_NOFOLLOW`（平台支持时）、私有模式和已有跨平台非阻塞锁原语；
2. State Root、Action Audit目录和数据库在POSIX保持私有权限；
3. 原始Owner/Operation Token不持久化、不进入`repr`；
4. Scan Report只保存Generation、计数、时间和摘要；
5. 不记录Action参数、路径、Session ID、Plan ID、Process ID、Artifact ID、异常正文或Secret；
6. Artifact扫描只读取`purpose/call_id`，不读取正文；
7. Fence不替代用户认证、数据库加密、恶意同UID隔离或不可抵赖签名。

## 后果

### 正面后果

- 独立Action HTTP/Worker继续保持删除，产品拓扑没有扩大；
- 两个产品宿主无法同时拥有同一State Root的Action组合根；
- 旧Owner和伪造/过期Operation能力不能提交Route终态；
- Execute之后的数据库故障、超时、取消和重启都不会自动重复写效果；
- Route期限、尝试和中断成为持久、可审计事实；
- 跨Store缺口被显式修复、报告或失败关闭，不再静默忽略；
- Router与Store按职责拆分，可读性门禁没有新增超大符号。

### 负面后果

- 每次Action增加一条Operation记录和至少一次写事务；
- 启动扫描成本随Route、Session和Action Artifact数量增长；
- 保守UNKNOWN和人工处置会降低部分故障场景的自动成功率；
- 多Store仍非原子，扫描只能修复可证明缺口；
- Python协作Deadline不能强杀无视取消的受信插件协程；
- 同UID恶意主体仍可能直接读取或篡改本地状态，需后续威胁加固。

## 被明确禁止的回退

1. 不得重新增加独立Action HTTP/Worker作为当前产品默认拓扑；
2. 不得在超时、断线、取消或新Owner接管后自动再次Execute写操作；
3. 不得把UNKNOWN改写为failed来获得“全部终态”指标；
4. 不得持久化或日志输出原始Owner/Operation Token；
5. 不得绕过`claim_operation/complete_operation`直接把产品Route写入执行终态；
6. 不得以启动扫描替代Session、Artifact或Process各自权威Store；
7. 不得把跨Store扫描描述成全局事务或Exactly Once外部效果。

## 验证与关闭条件

- [x] Action Audit v1→v2迁移与未知版本拒绝；
- [x] 产品写入强制Owner，Audit文件锁拒绝竞争进程；
- [x] 产品组合根文件锁拒绝第二宿主；
- [x] 新Generation拒绝旧Owner迟到提交；
- [x] 写效果超时进入UNKNOWN且只Reconcile一次；
- [x] Reconcile尝试有界，耗尽进入人工处置；
- [x] Executor返回后Audit故障，重启路径不重复Execute；
- [x] Cancel后UNKNOWN/Reconcile的Gateway回归；
- [x] Execution Plan单边缺失可修复且不执行效果；
- [x] 无Session引用Route和孤儿Action Artifact进入低敏报告；
- [x] Product Server持久化恢复扫描；
- [x] 本地`make check`：3595 passed、32 skipped；
- [ ] Linux Python 3.12/3.13、macOS、Windows、固定Container和Documentation CI全部通过；
- [ ] 0.9.3d在规模数据下冻结扫描延迟、Operation增长和UNKNOWN积压阈值。

## 后续工作

- 0.9.3d：长会话Soak、扫描/启动延迟、Operation增长、Process孤儿和UNKNOWN积压基线；
- 0.9.4：输入/输出预算、异常清洗、扩展边界、同UID威胁和低敏Telemetry；
- 0.9.5：用户可见恢复/人工处置UX和维护入口；
- 1.0：按真实C端规模评估是否需要多租户调度或远程控制面；如需要必须新立ADR，不能复活历史实现。
