# Harnessix Code 0.7 可信执行与工程交付设计

- 状态：实施中
- 更新日期：2026-09-08
- 适用范围：0.7.0～0.7.5
- 已完成切片：0.7.0、0.7.1

## 1. 目标与非目标

0.7 将 0.5/0.6 的可信工具准入升级为跨平台 Permission、Sandbox、Process、事务性交付和统一 Action 风险边界。目标是在明确能力声明内执行真实软件工程任务，并对取消、超时、崩溃和结果丢失给出可恢复事实。

本阶段不实现多租户云隔离、远程执行池、完整 TUI、MCP/Skill/Hook 产品协议或规模化 Eval；这些分别属于 0.8/0.9。0.4.3c 的真实 Provider 计价发布证据由 0.9 收口。

## 2. 总体架构

~~~text
Tool / MCP / Hook Adapter
          │
          ▼
  ExecutionIntent Normalizer
          │
          ▼
 Permission Planner ── Workspace Snapshot / Policy / Capability Probe
          │
          ▼
 Immutable ExecutionPlan ── Approval Checkpoint
          │
          ▼
 Executor Router
   ├── Host guarded
   ├── Host sandboxed (Seatbelt/Bubblewrap)
   ├── Container strong (Docker compatible)
   └── Durable Action Worker
          │
          ├── Process Supervisor / PTY / Output Artifact
          ├── Workspace Transaction / CAS / Git
          ├── Network Enforcement
          └── Secret Provider + Redactor
          │
          ▼
 Effect Ledger / Audit / Reconcile / Session Projection
~~~

## 3. 模块边界

| 模块 | 所有权 | 禁止承担 |
|---|---|---|
| `execution` | Intent/Plan/指纹/能力匹配 | 打开文件、启动进程 |
| `workspace` | 平台路径端口、Snapshot、资源身份 | Policy、模型参数解释 |
| `sandbox` | 后端探测、命令变换、能力证据 | 将 Host guarded 冒充 strong |
| `secrets` | SecretRef 解析、最小注入、Redactor | 把明文写入领域事件 |
| `processes` | owner、Lease、stdin/PTY、回收、输出 | 审批和自动重放 |
| `delivery` | staging、CAS、事务账本、Diff、恢复 | Push 或隐式覆盖脏文件 |
| `git` | 安全 Git argv、Checkpoint/Commit/Worktree | 通过 Shell 拼接或默认 Push |
| `action` | 外部效果租约、UNKNOWN/Reconcile | 本地 OS 隔离 |

## 4. 核心流程

### 4.1 规划与批准

1. Tool Adapter 校验输入 Schema，并生成规范 `ExecutionIntent`；
2. Workspace 端口解析逻辑路径，取得 root/cwd/resource snapshot；
3. Capability Probe 给出 requested/effective Sandbox、网络、PTY 和平台能力；
4. Policy 计算 deny/allow/require approval；
5. Planner 冻结 canonical `ExecutionPlan` 和 fingerprint；
6. 需要批准时持久化展示数据与同一 fingerprint；
7. 恢复或回复批准后，执行前重新观测所有绑定事实；不一致即计划过期。

### 4.2 执行与恢复

1. 先在 ledger 写入执行意图和 owner/transaction id；
2. 建立 Sandbox、Process Owner 或 Workspace Transaction；
3. 执行中持续发布有界事件，输出先 Redact 后进入 Artifact/Session；
4. 完成后记录外部可核对事实，再发布 Tool Result；
5. 结果丢失时根据 ledger 和真实世界观察归类；
6. 只有明确 `not_started` 才可由上层创建新计划，`unknown` 永不自动重放。

## 5. 安全级别

| 级别 | 文件系统 | 进程 | 网络 | 适用范围 |
|---|---|---|---|---|
| host_guarded | 应用层路径/审批 | Process Group/Job Object | Host 权限，不宣称隔离 | 用户信任命令、真实本地工具链 |
| host_sandboxed | Seatbelt/Bubblewrap | OS Sandbox + owner | none 或受管代理 | macOS/Linux 不可信命令 |
| container_strong | 只读 root + 显式 bind | 容器 PID/resource | none 或隔离代理 | 三平台默认强隔离 |

后端能力低于计划要求时，在 spawn 前失败关闭。用户若选择降级，必须形成新 Intent/Plan/Approval。

## 6. 数据与持久化

0.7 新记录遵循 append-only event/ledger：

- `ExecutionPlanRecord`：canonical plan、fingerprint、policy/capability evidence；
- `ProcessLeaseRecord`：owner token、状态、deadline、output artifact、reconcile；
- `WorkspaceTransactionRecord`：source snapshot、CAS manifest、cursor、成员观察；
- `GitDeliveryRecord`：base/tree/branch/worktree/commit/push action；
- `ExecutionAuditRecord`：不含 payload/secret 的统一审计索引。

SQLite 保持本地正式存储，所有 schema 迁移向前；旧 reader 不得接管新状态。大输出和完整 Diff 进入 Artifact，事件只保存引用与摘要。

## 7. 异常与恢复矩阵

| 切点 | 持久事实 | 恢复动作 | 禁止行为 |
|---|---|---|---|
| Plan 前 | 无 | 重新规划 | 推断已批准 |
| Approval 后、执行前 | Plan + decision | 重验 snapshot/capability | 参数变化后复用批准 |
| Process spawn 前 | intent | 标记 not_started | 用旧 PID 推断 |
| Process spawn 后、lease 前 | intent/owner token 可能不完整 | owner 核对，无法证明则 unknown | 自动重发命令 |
| 文件 replace 前 | transaction + temp identity | 清理已知 temp，保持 before | 跳过 source CAS |
| replace 后、cursor 前 | transaction + member intent | 比较 before/after CAS | 盲目继续下一个成员 |
| Commit 后、result 前 | expected tree/parent/message | 查询 HEAD/对象 | 再次 commit |
| Push 后、result 前 | remote/ref/expected oid | 查询远端 ref | 自动再次 push |

## 8. 测试策略

1. 契约：严格字段、canonical JSON、指纹逐字段变异；
2. 路径：POSIX symlink/mount/race；Windows drive/UNC/ADS/reserved/reparse/case/long path；
3. Sandbox：能力探测、不可用、禁网、只读根、资源限制和降级；
4. Process：双流、stdin、PTY、后台、子孙进程、取消、超时、宿主退出、输出 Artifact；
5. Secret：Canary 扫描 Context/Session/log/trace/diff/artifact/diagnostic；
6. Delivery：新增/修改/删除/重命名、脏冲突、每个 replace/commit/push 切点；
7. 集成：三平台真实临时 Git 仓库和 Container smoke；
8. 安全：Prompt Injection、Hook/config 注入、代理变量、DNS/IP 绕过、扩展直连 executor。

## 9. 发布切片

- 0.7.0：研究、差距、ADR、Threat Model v2；
- 0.7.1：平台路径、Workspace Snapshot、Execution Plan/Approval；
- 0.7.2：Sandbox/Network/Secret；
- 0.7.3：Process/PTY/后台监督；
- 0.7.4：Workspace Transaction/Git；
- 0.7.5：统一路由、攻击测试、真实仓库和发布门禁。

每片必须同时更新源码、Schema、迁移、总体/详细设计、测试记录和能力声明。没有三平台真实证据的能力不得写入 README 当前能力。

## 10. 0.7.1 跨平台Workspace与Permission详细设计

### 10.1 当前实现边界

0.7.1已经建立新可信执行面的基础契约，不直接替换0.5历史Patch/Process桥接。新0.7执行器必须消费`ExecutionPlan`；旧Session事件继续按原Schema读取，直至0.7.5由兼容Adapter统一路由。该分阶段策略避免把历史审批误写为已经绑定完整Workspace和环境事实。

当前代码边界如下：

| 模块 | 正式职责 | 当前保证 |
|---|---|---|
| `workspace.paths` | 模型逻辑路径规范化和平台比较键 | 只接受UTF-8、`/`分隔相对路径；拒绝绝对路径、回退段、控制字符和Windows特殊名字 |
| `workspace.snapshot` | 选择资源的有界快照和执行前复核 | 绑定root/cwd、文件内容、目录成员、缺失目标父目录和显式外部根 |
| `workspace.windows` | Windows原生句柄链 | 使用`CreateFileW`逐段拒绝Reparse Point，持有根句柄，支持扩展长度路径 |
| `workspace.leases` | 跨宿主写所有权 | SQLite租约、到期时间和单调fencing token；副作用端口提交前必须复核token |
| `execution.contracts` | Intent、能力、Sandbox、Policy、Plan和Approval | 严格冻结模型、规范SHA-256、逐字段能力与平台匹配 |
| `execution.store` | Plan/Approval持久检查点 | SQLite Schema v1、不可变写入、幂等重放、未知版本和损坏记录失败关闭 |

Workspace租约只协调遵守Harnessix协议的写入者，不能阻止外部编辑器修改文件。外部变化由执行前`WorkspaceSnapshot`复核检测；Host命令无法预知的任意写集合仍必须获得高风险批准，优先在后续Sandbox或事务性副本中执行。

### 10.2 逻辑路径与平台端口

领域路径统一为最多4096 UTF-8字节、128段、单段不超过平台上限的相对路径，`.`仅表示Workspace根。模型不能提交宿主绝对路径、盘符、UNC或设备路径；外部目录由宿主以不透明`location`绑定，计划只保留路径摘要、对象身份和访问模式。

POSIX端口复用已验证的root FD、`openat`和`O_NOFOLLOW`能力，并在0.7快照中要求所有资源段与Workspace根位于同一设备。符号链接、普通文件硬链接、跨挂载资源和检查期间对象变化失败关闭。需要访问其他文件系统时必须配置外部根。

Windows端口不模拟POSIX权限位：

- 拒绝盘符/UNC形式的模型路径、反斜线、ADS、尾随点或空格、DOS设备名及大小写折叠冲突；
- 宿主根可使用盘符或UNC，内部转换为`\\?\`扩展长度路径；
- 从卷/共享根开始逐段打开句柄，所有段使用`FILE_FLAG_OPEN_REPARSE_POINT`并拒绝Reparse Point/Junction；
- 共享模式不允许写入或删除，在观察窗口内阻止目标和已打开父段被替换；根句柄持续持有到端口关闭；
- 对象身份使用Volume Serial与File Index，内容变化另由属性、时间、大小及文件SHA-256/目录成员摘要绑定；普通文件硬链接数必须为一。

### 10.3 Workspace Snapshot

`harnessix.workspace-snapshot/v1`使用`selected-resources-sha256/v1`，不对整个大型仓库做无界遍历。每个Snapshot必含cwd目录观察，并按`location/path/access`规范排序。资源分为文件、目录和缺失目标：文件保存内容SHA-256；目录保存有界成员观察摘要；缺失目标保存现存父目录身份和目标名。

固定限制为单文件8 MiB、总观察正文32 MiB、目录10000项、资源256项、外部根16个。Snapshot不保存宿主根明文路径。执行前按原请求重新捕获，任何root、cwd、资源内容/身份、外部根或访问配置变化均返回`execution_plan_stale`。

### 10.4 不可变Execution Plan与Approval

`harnessix.execution-plan/v1`完整绑定：Tool来源、名称、版本、Tool指纹、规范参数、效果与风险、幂等键、Workspace Snapshot、环境值摘要、Secret名称/版本/目标、Sandbox/Network、Policy版本和执行器能力证据。环境和Secret按目标平台语义去重；Windows环境变量名大小写不敏感，普通环境不能覆盖Secret注入目标。

能力证据自身携带规范摘要，Plan要求Workspace平台、Sandbox级别、网络模式和能力摘要一致。需要审批时，`harnessix.execution-approval/v1`同时绑定`plan_id`与完整Plan fingerprint；拒绝、参数变化、环境变化、Secret版本变化、策略变化或能力变化均不能复用旧批准。

Plan与Approval写入独立私有SQLite存储。相同内容重复提交为幂等，不同内容复用Plan ID或二次改变审批结果为冲突；未知Schema版本、外键缺失和损坏JSON不进行推断恢复。该存储不包含环境值或Secret明文。

### 10.5 失败语义

| 错误 | 含义 | 可恢复动作 |
|---|---|---|
| `workspace_path_denied` | 路径、链接、挂载或对象类型越界 | 修正资源声明后重新规划 |
| `workspace_parent_missing` | 新文件目标的父目录未绑定 | 先显式创建/声明父目录 |
| `workspace_busy` | 其他owner持有有效租约 | 等待或由原owner释放 |
| `workspace_lease_lost` | 租约过期或fencing token落后 | 禁止提交，重新取得租约与计划 |
| `execution_plan_stale` | 任一执行事实与冻结计划不同 | 丢弃批准并生成新Plan |
| `execution_plan_conflict` | 同一Plan ID对应不同内容 | 调查调用方幂等冲突 |
| `approval_plan_mismatch` | 批准不属于当前待批Plan | 拒绝执行 |
| `execution_store_version` | 本地存储版本未知 | 使用匹配版本或正式迁移工具 |
| `execution_store_corrupt` | 持久记录不能通过严格契约 | 停止执行并保留数据库诊断 |

### 10.6 验证与后续接入

确定性测试覆盖路径属性、POSIX链接/外部根/缺失父目录、Windows Junction/长路径/根替换、文件与目录漂移、跨独立进程租约、fencing token、环境/Secret碰撞、Plan逐字段变异、Approval绑定、持久重开、重复写入、未知版本和损坏记录。Linux和macOS运行POSIX端口；Windows CI从本片开始实际运行原生Workspace测试，而非仅运行平台中立导入。

0.7.2～0.7.4新增的Sandbox、Process和Delivery端口必须先复核Plan、Approval、Workspace Snapshot和fencing token，再获得底层执行能力。0.7.5完成旧Patch/Process Adapter接入前，README不会把完整统一Permission入口声明为当前能力。
