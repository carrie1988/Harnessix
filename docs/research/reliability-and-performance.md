---
doc_type: source-research
status: current
version: 6
code_revision: 732278e1475dac6e391e81b152a3e73631ba3c5a
owners:
  - core
modules:
  - app_server
  - sdk
  - session
  - protocol
  - artifacts
  - trusted_actions
  - product_config
  - processes
related_adrs:
  - docs/adr/0089-bounded-local-transport-lifecycle.md
  - docs/adr/0090-plan-first-store-maintenance-and-backup.md
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
related_tests:
  - tests/app_server/test_server_sdk.py
  - tests/agent/test_session_contract.py
  - tests/protocol/test_requests.py
  - tests/trusted_actions/test_router.py
  - tests/agent/test_store_maintenance.py
  - tests/product_config/test_action_recovery.py
  - tests/product_config/test_server_and_cli.py
  - tests/context/test_compaction_runtime.py
  - tests/context/test_sources.py
  - tests/benchmarks/test_soak_long_session.py
supersedes: []
---

# 可靠性、背压与长期运行源码研究

## 1. 研究目标与固定基线

本文为路线图0.9.3冻结首轮源码事实，回答四个问题：

1. 本地Headless Agent在stdin仍打开而stdout阻塞或断裂时，能否有界退出；
2. Python SDK的并发请求、取消后迟到响应和关闭是否具有明确容量与所有权；
3. Session、Protocol Request、Artifact和Trusted Action长期增长目前有哪些真实边界；
4. 0.9.3应如何拆成可独立验收的纵向切片，而不是建立新的横向“性能框架”。

| 项目 | 固定Revision | 研究用途 |
|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | App Server Client背压、关闭与连接并发 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | Session/Server事件队列与取消边界的反例和取舍 |
| Claude Code逆向样本 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | Structured IO单Writer、迟到响应集合上限与输入关闭 |
| Harnessix修复前 | `6dd391a61e542158e0e46238a72f4a2c8775bb6b` | 0.9.2关闭后的现状与缺口 |
| Harnessix 0.9.3a | `f11359447f3bc68ffb97a100bb8b4bbcc1a891e5` | 本地传输最小正式修复 |
| Harnessix 0.9.3b | `cb3f3ea834624d5a8f84396952eba212650065d1` | 共库容量、Plan-first保留、备份与恢复 |
| Harnessix 0.9.3c | `0bc942bce8aeb22747a06515732936d1a312cd02` | Action双层Owner、持久Operation期限、只对账恢复与跨Store扫描 |

Claude Code仓库不是官方源码，只用于交叉佐证；OpenCode中出现的无界队列也不被当作可直接复制的生产建议。
本文只提炼行为边界，不复制参考实现。

## 2. Codex源码证据

### 2.1 命令通道有界，消费事件与请求等待解耦

Codex固定提交的
[`app-server-client/README.md`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server-client/README.md)
明确区分三类资源：命令队列和内嵌Runtime保持有界；调用方事件队列为了避免“未读通知挡住请求Response”而单独采用
无界队列；关闭则先请求优雅收敛，超过期限后中止Worker。这个设计不能证明无界事件内存安全，但证明背压必须按
依赖环路分别设计，不能把所有流量塞入同一个有界Queue后声称“更安全”。

[`app-server-client/src/lib.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server-client/src/lib.rs)
中的`InProcessAppServerClient::start`使用有界命令Channel，并把等待Request结果放入独立Task，让主Worker继续排空事件；
`shutdown`先发送Shutdown命令，再对Response和Worker分别做有界等待，最后Abort。可借鉴的是**所有权与关闭期限**，
不是Rust Channel类型本身。

### 2.2 连接Gate把“停止接收”与“等待已进入工作”分开

[`connection_rpc_gate.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server/src/connection_rpc_gate.rs)
在Shutdown后拒绝新的Run，同时等待已经取得许可的Future完成。相关测试覆盖Shutdown等待活动工作和拒绝迟到工作。
这支持Harnessix保持“关闭后不再受理，已持久事实按自己的恢复语义结算”，不支持把连接断开等同于业务Turn回滚。

## 3. OpenCode源码证据

OpenCode固定提交在Event HTTP Handler、LLM native runtime和PTY输出中使用`Queue.unbounded`，在Session abort路由中通过
显式Abort语义结束运行。该事实有两点价值：

1. UI事件、Provider事件和PTY字节属于不同流量域，不能只凭“使用Queue”推断统一容量合同；
2. 无界队列是需要另行证明消费速度和生命周期的债务，而不是Harnessix可以照搬的默认值。

因此0.9.3不以“与OpenCode一致”为由放开任何无界集合；Harnessix的本地Response归并仍要求数量上限，完整Session
长期增长则在持久化容量切片中单独解决。

## 4. Claude Code逆向样本证据

[`structuredIO.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/cli/structuredIO.ts)
显示Structured IO把所有Control Request与流事件放入同一个Outbound Stream，由唯一Drain路径写stdout，防止控制消息越过
已排队事件；输入关闭时拒绝全部Pending Request。它还把已结算Tool Use ID集合限制为1000并按插入顺序淘汰，明确说明
长期Session不能无限保留迟到/重复响应身份。

这支持Harnessix对Pending和Abandoned采用共享容量，但不支持简单TTL删除Abandoned：JSON-RPC迟到Response如果失去墓碑会被
误判为未知ID并污染同一连接。Harnessix 0.9.3a选择“取消后仍占用槽位，直到迟到Response或连接关闭”，在内存上有界，
同时保留协议安全性。

## 5. Harnessix修复前的源码事实

### 5.1 App Server stdio

修复前[`stdio.py`](../../src/harnessix/app_server/stdio.py)的结构是：

```text
async Reader -> process_frame -> asyncio.Queue -> async Writer -> to_thread(write + flush)
```

精确问题：

- `outbound_timeout_seconds`只约束Response进入Queue，不约束同步`write + flush`；
- 取消等待`asyncio.to_thread`不会终止底层同步写，线程池线程可继续阻塞；
- 主循环阻塞在另一个`to_thread(readline)`时，Writer失败无法主动唤醒；
- Pending Semaphore和Outbox在initialize前构造，客户端协商出的更小Limit没有实际约束运行对象；
- Dispatch设置Stopping后，主循环若正在等stdin也不能立即观察；
- 关闭Writer超时被吞掉，进程退出码无法区分正常EOF与输出通道失效。

这些问题不损坏已提交Session事实，但会让本地Agent进程无法按期退出，并允许协议声明与实际容量不一致。

### 5.2 SDK子进程传输

修复前[`agent_client.py`](../../src/harnessix/sdk/agent_client.py)把Subprocess Transport、Agent Client和Response解析集中在
同一文件。`_pending`和`_abandoned`均为无界集合；调用取消把ID移入Abandoned，但永不响应的Server会让集合持续增长。
`close`直接在调用方Task内执行，调用方取消后可能停在关闭stdin、等待进程或Reader Task之间。关闭超时硬编码为10秒和5秒，
没有低敏感度资源快照。

### 5.3 持久化和Trusted Action长期边界

源码与现行模块文档显示，0.9.3仍有以下未关闭项：

| 领域 | 已有边界 | 未关闭问题 |
|---|---|---|
| Session | WAL、FULL同步、单Runtime宿主锁、事务CAS、Plan-first离线维护 | 用户导出、在线调度、Checkpoint和Vacuum合同 |
| Protocol Request | 幂等Claim/Complete、参数指纹、终态Cutoff删除 | accepted目标恢复、历史分页和启动恢复扫描成本 |
| Artifact | 单件/Turn配额、TTL字段、分页读取、正文Tombstone与容量水位 | 默认调度、物理空间回收和Action/Process跨Store孤儿 |
| App Server | 单Thread Delta 1000条、Page Limit | Thread总数、总Delta字节、Replay/列表大历史性能 |
| Trusted Action | Hash链、UNKNOWN/Reconcile、产品启动恢复 | Owner fencing、跨Store孤儿扫描、Route超时与低基数信号 |
| Model | 单尝试预算、Provider错误分类 | 全局并发/速率限制、熔断和长运行压力基线 |

0.9.3a只关闭本地传输生命周期；0.9.3b进一步关闭三类共库事实的容量、保守清理和备份恢复实现，但不关闭上述
accepted恢复、物理空间回收、Trusted Action效果恢复和完整Soak问题。

### 5.4 0.9.3b实施前源码求证

#### Session数据库已经具备迁移和单宿主基础，但没有维护合同

[`session/sqlite.py`](../../src/harnessix/session/sqlite.py)证明Session初始化在`BEGIN IMMEDIATE`内校验Application ID、
连续Migration及其SHA-256，提交后才单独启用WAL；`runtime_owner()`使用数据库旁路文件锁阻止第二宿主。该事实支持在同一
SQLite内追加维护元数据，也说明Migration不能执行长时间正文扫描或自动清理。Runtime Owner Token只存在于Store实例，不能
替代同进程静默维护窗口。

#### Protocol账本不能精确回答accepted请求关联哪个业务对象

[`protocol/requests.py`](../../src/harnessix/protocol/requests.py)只持久化`client_instance_id/request_id/method/params_sha256`和
有界终态结果。原始Params被规范化后只留下不可逆摘要；不能从一个`accepted`行还原它是否正在创建Thread、推进Turn或发布
Artifact。因此“按Method猜测目标”或“accepted不影响其他表”都没有源码证据。0.9.3b采用全局保护Session/Artifact，接受
清理延迟来避免误删恢复事实。

#### Artifact已有TTL Tombstone，但缺少跨Store计划与发布时间

[`artifacts/sqlite.py`](../../src/harnessix/artifacts/sqlite.py)的现行`collect`只把到期Published正文转为Expired并清空Body，
不会删除Manifest；读取Expired固定失败，符合历史引用不变为空成功的要求。旧表只有`expires_at`，没有统一发布时间，且多个
专用发布器仍需保持同一列顺序。0.9.3b因此新增`created_at`并集中显式列写入，但清理资格继续使用既有`expires_at`，不把
回填时间当成真实发布时间。

#### 采用与拒绝

| 源码事实 | 采用 | 拒绝 |
|---|---|---|
| Session Migration和Runtime Owner已存在 | 复用共库、向前Migration和宿主锁 | 新建Maintenance服务或第二数据库 |
| Protocol只存Params摘要 | 任意accepted全局保护Session/Artifact | 反推目标ID或按Method启发式删除 |
| Artifact已有Tombstone状态 | 先清正文、保留Manifest；Thread整组删除时再移除 | TTL到期直接删除Manifest |
| 三类表共享SQLite事务 | Item变更与Progress游标同事务 | 内存游标或“删除后再记进度” |
| SQLite Backup API和原子替换可用 | 执行前Plan绑定完整备份 | 把导出SQL或文件复制建议当正式回滚 |
| Vacuum可能持长锁 | 0.9.3d单独测量与决策 | Agent热路径自动Vacuum |

### 5.5 0.9.3c实施前源码求证

#### Codex把超时、取消与进程树清理显式建模

Codex固定Revision `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`的
[`codex-rs/core/src/exec.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/exec.rs)
在145～245行附近显式组合超时、取消和执行结果，在1000～1069行附近使用TERM Grace、进程组Kill和有界输出Drain。
这证明“调用Future超时”与“底层进程已完成回收”是两个事实。Harnessix因此让Route Deadline覆盖固定Process Profile期限和
30秒清理余量，同时继续由Process Supervisor负责真正的进程树终止，不在Router复制第二套Process控制。

#### OpenCode把Abort能力传递到每个Tool执行

OpenCode固定Revision `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`的
[`packages/opencode/src/session/prompt.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/session/prompt.ts)
在323～374行附近把Abort信号传到Tool执行并更新取消状态，在813～827行附近为单个Tool建立AbortController和中断清理。
该事实支持取消必须贯穿Tool调用，但不证明取消已撤销外部效果。Harnessix对写操作取消先持久UNKNOWN，再向Agent上层传播取消。

#### Claude Code逆向样本把Tool参数视为敏感并使用显式Timeout Race

逆向样本固定Revision `2ca5ddabfed5f220812ea11f029eda03b21bc4c1`中，`src/services/tools/toolExecution.ts:1134-1157`
把Tool参数Telemetry设为显式选择，`src/services/mcp/client.ts:3054-3102`组合进度与显式Timeout Race。由于该仓库不是官方源码，
这里只把它作为“参数不应进入默认诊断、Timeout必须有稳定结算”的交叉佐证，不把其具体实现当成公共合同。

#### Harnessix现有Route与Store边界要求保守恢复

0.9.3c前的[`trusted_actions/router.py`](../../src/harnessix/trusted_actions/router.py)已在Executor前持久`running/reconciling`，
[`product_config/action_runtime.py`](../../src/harnessix/product_config/action_runtime.py)也已把遗留执行态收敛为UNKNOWN；但Action Audit
没有Owner Generation、Operation Deadline或Attempt，Execution Plan、Session、Artifact和Process Lease也没有统一扫描。
因此实现选择双层Owner、Operation账本和保守扫描，而不是恢复独立Action HTTP/Worker或伪造跨Store全局事务。

#### 采用与拒绝

| 源码事实 | 采用 | 拒绝 |
|---|---|---|
| Codex区分Future结果与进程回收 | Route期限覆盖Process清理余量 | Router直接控制第二套进程树 |
| OpenCode向Tool传递Abort | 取消后先持久保守结果再传播 | 把Cancel当作外部撤销证明 |
| Tool参数可能敏感 | Operation/Scan只保存身份摘要、计数和稳定Code | 保存参数、路径或异常正文便于调试 |
| Route已内嵌不可变Execution Plan | 缺失Plan可由Route确定性修复 | 冲突时任意覆盖一侧 |
| 外部效果不能加入SQLite事务 | Claim/Complete局部事务加UNKNOWN/Reconcile | 宣称Exactly Once或自动重Execute |
| 产品已删除独立Action服务 | Owner放入唯一Coding Agent组合根 | 重建HTTP/Worker作为恢复组件 |

## 6. 根因归纳

```mermaid
flowchart TD
    SyncIO[同步BinaryIO] --> Pool[to_thread线程池]
    Pool --> CancelGap[Task取消不等于系统调用终止]
    PreHandshake[握手前创建容量对象] --> LimitGap[协商值只返回未执行]
    Pending[Pending Future] --> Cancelled[调用方取消]
    Cancelled --> Late[仍可能收到迟到Response]
    Late --> Tombstone[必须保留身份墓碑]
    Tombstone --> Growth[无共享容量则无限增长]
    CallerClose[调用方拥有Close Task] --> CancelClose[取消可打断资源回收]
```

根因不是“Python性能差”，而是同步I/O所有权、协议迟到Response语义、协商时机和关闭Task所有权没有统一建模。

## 7. 采用与拒绝

### 7.1 采用

1. stdio使用单Reader守护线程和单Writer守护线程，事件循环只拥有有界Mailbox和Request Task；
2. Reader同时等待输入、Writer终结和Stopping，任一终结都能推动统一关闭；
3. Pending Semaphore在READY后按协商值创建，Outbox每次入队按当前协商Limit判断；
4. SDK Pending与Abandoned共享`max_pending_requests`槽位；取消不释放槽位，迟到Response或关闭才释放；
5. SDK Close由Transport拥有独立Task，公共`close`只Shield等待；
6. 提供不含Request ID、路径或stderr正文的`SubprocessTransportSnapshot`；
7. 将Subprocess实现拆到独立模块，保持`agent_client.py`职责和可读性门禁。
8. 共库容量报告同时记录逻辑行、正文和数据库/WAL字节，并在读取时验证数据完整性；
9. 删除前持久化不可变Plan、按序Item和独立Progress；每批业务变更与游标同事务；
10. 任意`accepted`请求全局保护Session/Artifact，执行时再次核对候选；
11. 维护执行前强制创建Plan绑定备份，崩溃后复用同一Backup和Ordinal；
12. Artifact正文先Tombstone，Thread满足全套禁删条件后才整组删除。
13. Product Action Runtime在打开任一Action Store/Process Owner前取得最外层跨进程锁；
14. Action Audit每次接管递增Generation，所有产品写入校验持久Fence；
15. Execute/Reconcile以持久Operation记录Phase、Attempt、Owner、Deadline和状态；
16. Claim与执行态、Complete与终态分别在同一SQLite事务提交；
17. 写超时/取消/异常进入UNKNOWN，恢复只调用有界Reconcile；
18. 启动扫描Plan、Route、Session、Artifact和Process Lease，只修复可证明缺口并持久低敏报告。

### 7.2 拒绝

| 方案 | 拒绝原因 |
|---|---|
| 继续用`to_thread`，只扩大Timeout | 不能终止已经阻塞的同步系统调用 |
| 取消Request时立即释放槽位并删除ID | 迟到Response会成为未知ID，可能使整连接失败 |
| 超过Abandoned上限后静默淘汰最旧ID | 同样破坏迟到Response归并安全 |
| Close捕获并吞掉`CancelledError` | 违反调用方取消语义，且无法证明清理一定完成 |
| 为性能另建独立HTTP/Worker服务 | 重新引入已在0.9.1f3删除的割裂产品边界 |
| 本切片同时增加Session GC和Action租约 | 失败语义、数据迁移和回滚单元过大 |
| 从Protocol参数摘要推断accepted目标 | 摘要不可逆，没有源码事实支持 |
| 候选变化后自动补选另一行 | 破坏Plan可审计性，恢复结果不可重算 |
| 升级或启动时自动清理 | 把普通启动变为破坏性操作，缺少审核和回滚窗口 |
| Maintenance内自动Vacuum | 锁时长、额外磁盘和三平台性能尚未建立证据 |
| 恢复独立Action HTTP/Worker | 扩大产品入口、认证和部署面，违背ADR 0081 |
| 仅依赖文件锁而无Generation | 新Owner接管后不能拒绝旧对象迟到提交 |
| 超时后把写效果标记failed并重试 | 不能证明外部写未发生，可能重复真实效果 |
| 重启时直接再次Execute | 非幂等Action可能重复，必须UNKNOWN后只对账 |
| 把跨Store扫描描述为全局事务 | 各Store和外部效果没有共同原子提交点 |

## 8. 0.9.3切片结论

| 切片 | 正式范围 | 关闭证据 |
|---|---|---|
| 0.9.3a | 本地stdio/SDK背压、Writer故障、取消后迟到Response、取消安全关闭、资源快照 | 代码、故障回归、三平台CI |
| 0.9.3b | Session/Protocol/Artifact容量快照、保留计划、清理与崩溃恢复 | Migration、前后水位、故障注入、备份恢复 |
| 0.9.3c | Trusted Action/Process Owner fencing、孤儿扫描、Route超时和对账 | 双进程竞争、崩溃窗口、UNKNOWN不误执行 |
| 0.9.3d | 长会话Soak、并发、内存、启动时延和数据库增长发布基线 | 固定场景、机器信息、阈值、原始低敏证据 |

0.9.3总项只有a～d全部完成后才能关闭。单次Microbenchmark或单平台“运行没报错”不能替代Soak和故障证据。

## 9. 源码与测试映射

| 结论 | Harnessix源码 | 测试 |
|---|---|---|
| stdio同步I/O泵与故障唤醒 | [`app_server/stdio.py`](../../src/harnessix/app_server/stdio.py) | [`test_server_sdk.py`](../../tests/app_server/test_server_sdk.py)中的Writer故障、慢输出和并发Limit用例 |
| SDK共享容量与迟到Response | [`sdk/subprocess.py`](../../src/harnessix/sdk/subprocess.py) | 同文件中的取消、乱序、Malformed和容量用例 |
| Close独立Task与Shield | [`sdk/subprocess.py`](../../src/harnessix/sdk/subprocess.py) | `test_subprocess_transport_close_continues_after_caller_cancel` |
| AgentClient职责保持 | [`sdk/agent_client.py`](../../src/harnessix/sdk/agent_client.py) | App Server与Product UI回归 |
| 共库容量与完整性扫描 | [`session/capacity.py`](../../src/harnessix/session/capacity.py) | [`test_store_maintenance.py`](../../tests/agent/test_store_maintenance.py)低敏容量用例 |
| 不可变Plan和禁删集合 | [`session/maintenance_records.py`](../../src/harnessix/session/maintenance_records.py)、[`session/maintenance_planning.py`](../../src/harnessix/session/maintenance_planning.py) | Plan篡改、accepted全局保护、Plan后新增pending用例 |
| 批次原子恢复 | [`session/maintenance_execution.py`](../../src/harnessix/session/maintenance_execution.py) | 单Item批次提交故障与Changed Candidate Skip用例 |
| 备份与原子Restore | [`session/maintenance_backup.py`](../../src/harnessix/session/maintenance_backup.py) | 备份发布后故障复用及完整恢复用例 |
| Migration 26与Artifact写入 | [`0026_store_maintenance.sql`](../../src/harnessix/session/migrations/0026_store_maintenance.sql)、[`artifacts/persistence.py`](../../src/harnessix/artifacts/persistence.py) | Session/Artifact升级与发布回归 |
| Product与Audit双层Owner | [`product_config/action_owner.py`](../../src/harnessix/product_config/action_owner.py)、[`trusted_actions/ownership_store.py`](../../src/harnessix/trusted_actions/ownership_store.py) | [`test_action_recovery.py`](../../tests/product_config/test_action_recovery.py)、[`test_router.py`](../../tests/trusted_actions/test_router.py)中的竞争进程与旧Fence用例 |
| Operation Deadline与原子结算 | [`trusted_actions/operation_router.py`](../../src/harnessix/trusted_actions/operation_router.py)、[`trusted_actions/operation_store.py`](../../src/harnessix/trusted_actions/operation_store.py) | 写超时、有界Reconcile、Audit故障后零重复Execute用例 |
| 跨Store恢复扫描 | [`product_config/action_recovery.py`](../../src/harnessix/product_config/action_recovery.py) | Plan修复、Route/Artifact孤儿和Product Server持久化用例 |

## 10. 0.9.3d长会话与性能证据专项源码核查

### 10.1 参考项目如何把性能运行变成可归因证据

| 项目与固定源码 | 已确认的机制 | 本项目取舍 |
|---|---|---|
| [Codex `e2e_benchmark.bzl:7-56`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/bazel/rules/e2e_benchmark.bzl#L7-L56) | 基准目标显式声明运行二进制、数据依赖和仓库根，标为手动执行；基准源与普通测试源分离 | 0.9.3d使用独立负载入口和固定环境Manifest，不把长Soak塞进每次快速单元测试，也不允许从调用机隐式继承数据集 |
| [OpenCode `benchmark.ts:17-50`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/app/e2e/performance/benchmark.ts#L17-L50) | 每次Benchmark必须恰好报告一次，缺失指标会使测试失败；输出带Schema、Run ID、平台、状态和重试信息 | 结果/Manifest必须严格校验，缺失样本、未知状态或重复报告失败关闭；不能把无指标的“成功退出”算通过 |
| [OpenCode `bench-test-suite.ts:4-52`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/script/bench-test-suite.ts#L4-L52) | 预热次数和测量次数分离；失败立即停止；输出Median/Best/Worst | 固定预热与正式样本，原始样本进入可重算低敏证据；不只保存单一P95，也不以一次运行直接制定通过阈值 |

这些源码提供的是证据组织方式，不证明其他项目已有Harnessix需要的长Session、Action故障和跨Store恢复覆盖；本项目不复制
其框架或声称横向性能优劣。

### 10.2 当前产品的增长路径与待实测假设

| 入口 | 当前源码事实 | 0.9.3d必须测量/判定 |
|---|---|---|
| [`AgentRuntime.__aenter__`](../../src/harnessix/agent/runtime.py#L316-L334) | 启动时遍历全部Thread，逐个读取完整快照并检查活动Turn | 500 Thread和长历史下的冷/热启动P50/P95、内存峰值；不得把扫描耗时隐藏在Provider时间中 |
| [`AgentApplicationService.list_threads`](../../src/harnessix/app_server/service.py#L235-L255) | 先读取并投影全部Thread，再按`limit`截取；`archived`过滤也在全量读取后 | 500/5000 Thread下分页时延与RSS；若超发布阈值，应设计Store侧分页及游标/Archive一致性，不得只扩大超时 |
| [`scan_product_action_recovery`](../../src/harnessix/product_config/action_recovery.py#L46-L109) | 启动时全量装载Route、Session引用和Action Artifact索引；只聚合低敏计数 | Route/Item/Artifact分别增长时的启动时延、峰值RSS、UNKNOWN积压和孤儿计数；必要时转为有界分页扫描但保持完整性结论 |
| [`SQLiteActionAuditStore.routes`](../../src/harnessix/trusted_actions/operation_store.py#L305-L314) | 返回全量Route元组并逐项`load`；方法名中的“有界”不代表结果集有容量上限 | 实测内存与数据库I/O，若需分页须保持稳定顺序、故障恢复游标和跨页不漏扫 |
| [`SQLiteArtifactStore.action_recovery_inventory`](../../src/harnessix/artifacts/action_output_store.py#L51-L66) | 一次返回全部Action Artifact身份元组 | 大量Artifact时的内存、扫描时延；报告仍只能公开计数，不能公开Call ID |

上述是由控制流推导的**待测量风险**，不是已经观察到的性能回归。0.9.3d首先冻结场景、样本、环境和失败规则，完成
实际Soak后才决定是否对特定路径做结构优化；即使优化，也必须以现有完整性与恢复测试作回归门禁。

### 10.3 三平台峰值RSS采集单位核查

- [Linux `getrusage(2)`](https://man7.org/linux/man-pages/man2/getrusage.2.html)明确`ru_maxrss`为KiB；Python
  [`resource.getrusage`](https://docs.python.org/3/library/resource.html#resource.getrusage)直接返回底层字段，不替各平台统一单位。
- [Apple归档手册](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/getrusage.2.html)
  将`ru_maxrss`写为kilobytes；但在2026-09-22本机macOS Python 进程中，`ru_maxrss=18890752`，同时`ps rss=18496 KiB`，
  两者按**字节**换算接近。归档手册与当前观测不一致，不能仅凭文档假定macOS单位；正式采集器必须固定平台实现、
  记录采集来源与单位，并在发布环境与独立进程视图交叉校验，无法判定时失败关闭。
- Windows官方[`GetProcessMemoryInfo`](https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getprocessmemoryinfo)
  返回`PROCESS_MEMORY_COUNTERS`；[`PeakWorkingSetSize`](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters)
  的单位是字节。0.9.3d必须在Windows原生CI/发布环境验证`ctypes`结构大小、调用结果和退出路径，不用Unix
  `resource`替代。

这只是单位与采集源核查，不是0.9.3d正式RSS基线或三平台Soak证据。

### 10.4 千Turn诊断后的Context与Compaction覆盖缺口

[macOS千Turn规模诊断](../validation/soak-macos-2026-09-23-v2/README.md)证明当前
`core_runtime`可在同一Thread完成1000个Turn，并产生可重算的时延、RSS、DB水位和Attempt事实；它没有证明
Context规划或压缩。源码证据如下：

| 源码/测试入口 | 已核验事实 | 对Soak合同的影响 |
|---|---|---|
| [`run_long_session`](../../scripts/soak_long_session.py) | 构造`AgentRuntime(store, provider)`，没有注入`context`、`async_context`、`compaction`或`summary_provider` | 每轮会准备模型历史，但不生成`ContextPrepared`；不触发自动Compaction。不能把1000次模型请求计数当作Context覆盖数。 |
| [`AgentRuntime.__init__`](../../src/harnessix/agent/runtime.py)与[`validate_runtime_switches`](../../src/harnessix/agent/runtime_configuration.py) | `compaction`与`summary_provider`必须成对配置；缺一个立即失败关闭 | 不能只打开阈值开关而继续复用未装配的Provider。 |
| [`prepare_and_commit_model_history`](../../src/harnessix/agent/model_history_runtime.py) | 只有显式Compaction配置存在，历史超过触发字节阈值或Provider报告溢出后才调用`_run_compaction` | 需冻结触发窗口、目标预算和预期压缩次数；增大Context窗口以避免触发不构成压缩验收。 |
| [`AgentRuntime._summary_text`](../../src/harnessix/agent/runtime.py) | 摘要Provider首事件必须是`ModelAttemptStarted`；完整响应还要求持久`ModelUsageObserved`、`ModelAttemptFinished`和与之相符的`ResponseCompleted.usage` | 现有[`SoakProvider.stream`](../../scripts/soak_provider.py)首事件为`ResponseStarted`且无用量事件，不能直接充当摘要Provider；必须提供不保留请求正文、但具备完整尝试/用量账本的确定性摘要夹具。 |
| [`test_runtime_refreshes_and_persists_source_freshness_each_model_step`](../../tests/context/test_sources.py)与[`test_proactive_compaction_uses_accounted_toolless_request_and_active_window`](../../tests/context/test_compaction_runtime.py) | Context检查和Compaction激活均有持久事件/投影的可核对测试路径 | Soak应从真实Session读取每轮检查、压缩计划/尝试/窗口，并复核Event Replay，而非只检查运行时内存计数。 |
| [`SoakManifest`](../../scripts/soak_manifest.py)、[`SCENARIO_METRICS`](../../scripts/soak_samples.py)和[`read_published_run`](../../scripts/soak_evidence.py) | v1长会话只声明`turn_local/rss_peak`；Manifest Provider只记录普通请求计数；Reader要求Manifest规范字节与落盘原文完全相同 | v1原件缺少Context/Compaction覆盖证明。直接增加默认字段会改变v1规范序列化并使历史Run读取失败；不能无版本地把旧诊断证据升格。 |

因此下一轮设计必须先解决**版本化证据合同**，再运行新负载：保留v1历史Reader；为具有明确Context窗口、
摘要Provider尝试/用量、检查次数、压缩次数和活动窗口身份核验的新场景建立可独立验证的合同。不得只增加一个
`assert len(turn.compactions) > 0`后沿用v1 Manifest，因为临时Session删除后原始数值证据仍无法证明此断言。
具体采用新的Scenario/Manifest版本，还是单独的受摘要绑定证明文件，需要在专项详设中评审；两种方案都须保留
低敏字段白名单和旧Run可读，不从性能结果反推阈值。

## 11. 研究边界

- 未在本研究中测量跨机器绝对性能；阈值必须由0.9.3d固定环境实测产生；
- 守护线程解决进程退出所有权，不保证底层第三方`BinaryIO.write`可被强制中断；阻塞写不再阻止主流程退出；
- SDK只管理直接App Server子进程，不拥有其任意后代进程树；发行物进程树归0.9.5；
- Snapshot是本地诊断事实，不是业务Session、身份认证或Telemetry上传合同；
- 远程Agent Protocol、HTTP Gateway和多租户不在0.9.3范围内。
- 0.9.3b只证明逻辑清理、崩溃恢复和备份边界；未证明SQLite物理文件缩小、在线Maintenance或用户级数据删除；
- `accepted`全局保护是由现行不可逆参数摘要推导出的保守结论，不表示Protocol已完成孤儿恢复。
- 0.9.3c只证明单State Root单产品宿主、局部事务和零自动重Execute；不证明外部效果Exactly Once或恶意同UID隔离；
- 0.9.3c启动扫描当前遍历完整Session和Route集合，规模时延、Operation归档和UNKNOWN告警必须由0.9.3d实测。
