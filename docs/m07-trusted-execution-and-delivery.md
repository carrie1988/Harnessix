# Harnessix Code 0.7 可信执行与工程交付设计

- 状态：已完成（2026-09-09）
- 更新日期：2026-09-09
- 适用范围：0.7.0～0.7.5
- 已完成切片：0.7.0、0.7.1、0.7.2、0.7.3、0.7.4、0.7.5

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

POSIX目录的持久身份绑定device/inode、类型/权限、link count和owner，直接成员摘要绑定名称、类型与device/inode。目录扫描仍在持有FD期间比较完整revision；因此扫描中的增删、替换或权限变化失败关闭，但未被显式选择的成员仅修改正文不会把“选择资源Snapshot”扩大为隐式全仓内容锁。

Windows端口不模拟POSIX权限位：

- 拒绝盘符/UNC形式的模型路径、反斜线、ADS、尾随点或空格、DOS设备名及大小写折叠冲突；
- 宿主根可使用盘符或UNC，内部转换为`\\?\`扩展长度路径；
- 从卷/共享根开始逐段打开句柄，所有段使用`FILE_FLAG_OPEN_REPARSE_POINT`并拒绝Reparse Point/Junction；
- 共享模式不允许写入或删除，在观察窗口内阻止目标和已打开父段被替换；根句柄持续持有到端口关闭；
- 对象身份使用Volume Serial与File Index；文件持久身份绑定只读属性、link count、大小及内容SHA-256，目录成员使用Python 3.12当前`st_dev/st_ino`而不是`DirEntry.stat()`缓存的时间/大小。完整属性、写入时间和大小只用于同一次句柄观察前后竞态比较，不作为跨观察的易变身份；普通文件硬链接数必须为一。

该取舍依据[Microsoft `BY_HANDLE_FILE_INFORMATION`](https://learn.microsoft.com/zh-cn/windows/win32/api/fileapi/ns-fileapi-by_handle_file_information)对Volume Serial/File Index对象身份及文件时间文件系统差异的定义，以及[Python 3.12 `os.stat_result`](https://docs.python.org/3.12/library/os.html#os.stat_result)在Windows以File Index提供`st_ino`的合同。时间戳仍是观察窗口内的竞态信号，但不是目录成员跨观察的主键。

### 10.3 Workspace Snapshot

`harnessix.workspace-snapshot/v1`使用`selected-resources-sha256/v1`，不对整个大型仓库做无界遍历。每个Snapshot必含cwd目录观察，并按`location/path/access`规范排序。资源分为文件、目录和缺失目标：文件保存内容SHA-256；目录保存有界直接成员的名称、类型与对象身份摘要，不递归绑定未选择成员正文；缺失目标保存现存父目录稳定身份和目标名。目录同名成员被替换会因对象身份变化而过期，未选择成员只改正文不会产生与计划无关的伪过期。

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

## 11. 0.7.2 Sandbox、网络与Secret详细设计

### 11.1 实现边界与安全等级

0.7.2实现Sandbox执行适配层、网络出口授权和Secret生命周期，不提前拥有通用进程生命周期。`ContainerCommandBuilder`只在全部事实复核后生成固定Docker兼容argv和短生命周期环境；0.7.3的Process Supervisor负责实际spawn、双流、超时、取消、进程树回收和持久Lease。真实容器验收直接执行Builder产物，只用于证明隔离参数有效，不能解释为模型调用已经接入统一执行路由。

安全等级保持三个独立语义：

| 等级 | 当前实现 | 不作出的保证 |
|---|---|---|
| `host_guarded` | 可声明Permission、Approval、环境最小化和后续Process owner | 不提供文件或网络强隔离 |
| `host_sandboxed` | macOS Seatbelt或Linux Bubblewrap通过实际deny-default预检后才广告 | Windows不宣称native host strong；只有二进制存在不足以通过探测 |
| `container_strong` | Docker/Podman兼容引擎证据、固定摘要镜像、只读根、显式Workspace mount、非root、cap-drop、no-new-privileges、IPC/PID/CPU/内存/tmpfs/nofile和网络策略 | 容器引擎控制面仍是受信高权限边界；不抵御宿主内核或Daemon失陷 |

后端不可用、版本变化、可执行文件身份变化或能力低于Plan要求时失败关闭。降级必须重新生成Intent、Plan和Approval，Builder没有Host fallback分支。

### 11.2 Execution Plan v2与能力证据

冻结的`ExecutionPlan v1`和`ExecutionCapabilityEvidence v1`保持字节兼容。Container切片新增`ExecutionCapabilityEvidence v2`、`SandboxBindingV2`和`ExecutionPlan v2`：

- 能力证据除平台、后端版本、Sandbox/Network集合和PTY/后台/进程树能力外，绑定自校验的`ContainerEngineProbe.digest`；
- Sandbox绑定不可变`ContainerSandboxProfile.digest`；Profile继续绑定网络快照和选择性出口网关实现摘要；
- Intent参数必须逐字段等于`ContainerCommandSpec`，命令摘要再绑定Profile摘要；
- 执行前重新计算Plan fingerprint、Workspace Snapshot、环境值摘要、Secret名称/版本/目标、能力证据、Profile和命令；任何变化不能复用批准；
- SQLite Execution Plan Store v1以严格union读取v1/v2，不迁移或改写既有v1记录。

公开JSON Schema新增Execution Plan/Capability v2及Sandbox、Network、Probe、Command和Egress契约。v1 Schema在连续生成中保持原SHA-256，避免把0.7.2字段静默写入历史合同。

### 11.3 Container启动适配

Container引擎由宿主绑定绝对普通可执行文件。探测使用固定`version --format`取得客户端/服务端版本，并使用固定`info --format`读取Docker Security Options或Podman rootless事实；命令运行在清理后的环境中，输出和时限有界。Builder初始化时保存可执行文件对象身份，prepare时再次比较设备/inode/大小/时间/模式和能力摘要。

固定启动参数包括：

- `run --rm --init --pull never`，镜像必须是`sha256:`摘要或`name@sha256:`；
- `--read-only --cap-drop ALL --security-opt no-new-privileges --ipc none`；
- 数字非root用户、固定`/workspace`工作目录、显式只读或读写bind；
- PID、内存、CPU、只读根、`/tmp` noexec/nosuid/nodev配额、nofile和stop timeout；
- 仅使用`--env NAME`传递获准变量名，Secret值绝不进入argv、Plan或对象repr；
- Profile声明的根读写Permission必须出现在Workspace Snapshot；外部根在正式挂载合同交付前失败关闭。

### 11.4 网络策略与受管出口

`NetworkPolicy v1`只接受规范精确域名或CIDR、`https`/`tcp`和唯一有序端口。域名规则必须是HTTPS；`limited`默认只接受公网解析，`restricted`可显式允许私网。DNS规划时解析为最多16个规范IPv4/IPv6地址，TTL限制1～300秒；出口连接只使用冻结地址，过期不动态刷新。

| 模式 | Container落实方式 | 失败条件 |
|---|---|---|
| `none` | `--network none` | 携带任何Egress binding |
| `full` | 普通bridge，仍需Policy显式允许 | 伪装为选择性代理 |
| `limited/restricted` | 仅连接带Policy/Gateway标签的internal bridge；代理环境由Builder独占 | 缺少匹配证明、标签/驱动/容器集合变化或代理配置冲突 |

受管网关只接受HTTP CONNECT。目标必须与批准的host/protocol/port及固定IP一致；域名HTTPS在向上游发送首字节前解析完整有界TLS ClientHello并要求精确规范SNI，无SNI、ECH不可见身份或不匹配均关闭连接。网关限制请求头、连接时间、空闲时间和双向字节数。内部网络证明严格解析Docker inspect JSON，拒绝重复键、非internal bridge、错误标签和除`harnessix-egress`外的任意已连接容器。

Docker API/Socket具有宿主级高权限，不进入不可信工作负载。0.7.3的生命周期管理器必须在spawn前立即重新inspect并核对`internal_network_attestation`；当前Builder只消费已经证明的binding，不创建网络或代理。

### 11.5 Secret生命周期和输出边界

`EnvironmentSecretProvider`是首个宿主适配：配置只保存受批准Secret名称、版本和宿主环境变量名。解析时验证名称、版本和注入目标，最多32项、合计64 KiB；Windows目标按大小写不敏感去重。明文仅存在于`ResolvedSecretEnvironment`的可变字节副本，并在作用域关闭时尽力清零；Python字符串和子进程环境产生的不可变副本不能承诺内存级彻底擦除。

`StreamingSecretRedactor`保留最长模式窗口，覆盖任意stdout/stderr chunk边界，以及原文、标准/URL-safe Base64、有无padding、URL百分号、hex、JSON字符串和Shell引用表示。Secret短于4字节或模式超过256项时拒绝启动可发布输出流。`SecretLeakGuard`为JSON/模型/日志/Artifact边界提供最多16 MiB、100000节点、64层的最终扫描；值可替换为`[REDACTED]`，键命中、不可序列化、超限或仍有Canary时阻止发布。

Redactor不是加密/DLP系统，不能检测哈希、压缩、分段重编码或语义推断。安全性依赖最小注入、网络隔离和所有持久/模型出口在0.7.5统一接入Guard；0.7.2只交付可复用正式端口和Container smoke接线。

### 11.6 持久化、失败语义与恢复

`SQLiteSandboxProfileStore`使用私有目录、0600数据库、WAL和FULL同步，按Profile内容摘要不可变保存；同摘要同内容幂等，不同内容冲突，未知Schema和损坏JSON失败关闭。它不保存宿主路径、环境值或Secret明文。Plan/Approval继续由Execution Plan Store持久化，网关实例、解析后Secret和Prepared launch不持久化。

| 错误 | 含义 | 恢复动作 |
|---|---|---|
| `sandbox_unavailable` | 引擎/Daemon/Host Sandbox探测失败 | 修复后端并重新探测、规划 |
| `sandbox_binding_changed` | 已绑定容器可执行文件对象变化 | 停止执行，重新绑定受信程序 |
| `sandbox_capability_mismatch` | Plan、Profile、Command或能力证据不一致 | 丢弃原批准并生成新Plan |
| `network_policy_unenforceable` | 请求网络语义无法由后端完整落实 | 不启动工作负载；修复内部网络/网关 |
| `network_destination_denied` | host/IP/protocol/port/TTL不在批准快照 | 拒绝连接；需要新网络Plan |
| `network_tls_identity_denied` | TLS身份不可见或SNI不匹配 | 关闭连接，不转发应用数据 |
| `secret_unavailable` / `secret_version_changed` | Secret缺失或版本漂移 | 不启动；重新配置或重新批准版本 |
| `secret_redaction_unsafe` / `secret_redaction_failed` | 无法建立可靠脱敏或最终扫描失败 | 阻止输出发布；效果状态仍按真实执行记录 |
| `sandbox_store_corrupt` | Profile持久记录不再满足严格合同 | 停止执行并保留数据库诊断 |

### 11.7 验证证据与剩余工作

确定性测试覆盖网络合同、DNS固定/过期/私网、CIDR、TLS ClientHello/SNI、真实asyncio代理中继、Docker inspect重复键/标签/额外容器、引擎和Host Sandbox探测、Profile持久化、Plan v2持久重开与逐字段漂移、Container argv、Secret版本/Windows碰撞、所有chunk边界和常见编码Canary。Linux CI额外拉取固定BusyBox OCI摘要，实际验证非root、零Capability、只读根、只读Workspace、仅loopback网络、可写`/tmp`以及Secret不进入argv且输出被脱敏；CPU/内存/PID/tmpfs上限的精确argv由确定性测试验证，压力与超限终止测试归入0.7.3 Process Supervisor。

Windows和macOS运行相同Sandbox/Secret合同与平台能力测试；Windows当前没有native host strong执行器，强隔离仍依赖通过探测的Docker Desktop/受管WSL2容器后端。0.7.3必须接入通用Process Supervisor、PTY、后台Lease、立即网络复核和Secret流式发布；0.7.5再把所有内置Tool与扩展统一路由到该边界。上述后续工作完成前，不宣称任意模型命令已经具备端到端生产隔离。

## 12. 0.7.3 跨平台Process与终端监督详细设计

### 12.1 已冻结的ProcessSpec、能力与Lease账本

0.7.3首先落地不依赖具体spawn API的领域合同和持久状态机，避免把旧`HostProcessRuntime`的POSIX前台行为直接扩散到Windows和后台任务：

- `ProcessSpec v1`使用稳定`process_id`和自摘要，分别表达`argv`、`posix_sh`、`cmd`、`powershell`；Shell source与argv互斥，原文参与Execution Plan批准，不由Runtime拼接；
- pipe/PTY、stdin关闭/管道、foreground/background、运行时限、输入/输出预算和终端尺寸均为计划字段；stdin默认关闭且预算为零；
- `ProcessCapabilityProbe v1`固定平台owner为POSIX Session或Windows Job Object，声明可用调用模式、PTY、后台和原子进程树归属，并绑定实现摘要；
- Planner要求ProcessSpec完整JSON等于`ExecutionIntent.arguments`，Process能力摘要等于`ExecutionCapabilityEvidenceV2.provider_evidence_digest`，并校验平台、PTY、后台和进程树能力；
- `ProcessLease v1`不可变绑定plan id/fingerprint、spec/capability摘要、lifecycle、随机owner token和deadline。数字PID只作观察字段，不构成恢复权限；
- 状态机以`prepared → starting → running → stopping → exited`为正常主线；未创建目标进程的确定性启动失败进入`failed`，任一非终态可按证据进入`unknown`，终态不可重开；运行身份必须以owner identity、PID和started time完整出现或全部缺失；
- stdout/stderr只保存观察字节数、实际持久字节数、完整流与持久前缀各自的SHA-256、截断和EOF；正文由私有有界输出Artifact保存，不进入Lease事件。

`SQLiteProcessLeaseStore`使用私有SQLite/WAL/FULL同步保存当前投影和append-only完整Lease事件。创建相同Lease幂等；状态推进同时校验完整绑定、相邻sequence、允许迁移及当前payload CAS。读取时交叉校验关系列、当前payload和同sequence事件，任何索引漂移、事件缺失、损坏JSON或未知Schema失败关闭。`active()`先验证所有记录再按payload状态过滤，不能通过篡改冗余state列隐藏待恢复进程。

本小节只完成领域契约、Schema、Planner和账本，不启动进程。下一小节实现POSIX owner worker、持久输出和pipe/PTY控制；随后实现Windows suspended spawn + Job Object与ConPTY，最后接入Container launch、取消/超时/宿主死亡恢复。上述执行与三平台故障门禁完成前，0.7.3保持未完成。

### 12.2 独立owner、终端与宿主死亡监督

Process Supervisor不直接把目标进程作为Agent服务的普通子进程。每次启动创建仅当前用户可访问的独立run目录、随机256位owner token和owner identity，再通过不可继承到目标进程的匿名控制管道启动短生命周期owner worker。启动帧携带已批准argv、cwd、精确环境、Secret目标名、deadline和I/O预算；帧只存在于内存管道，Secret明文不写入Plan、Lease、回执、命令行或run目录。目标环境不合并owner或Agent宿主环境。

owner先写输出文件并`fsync`，随后原子替换HMAC-SHA256回执；MAC使用Lease中的owner token，绑定process id、owner identity、目标PID、时间、终态和双流摘要。恢复端只消费可验证回执，数字PID不构成控制权限。当前投影与append-only Lease事件继续CAS推进；运行中输出检查点允许同状态递增sequence，`failed/exited/unknown`终态不可重开。宿主重启遇到`prepared`可确定为未启动；`starting/running/stopping`先等待owner完成控制管道EOF清理，只在无终态回执时进入`unknown/host_lost`，禁止按PID补发、发送输入或终止其他进程。

POSIX目标在独立Session/Process Group中启动。owner监视控制EOF、deadline、根进程、完整进程组及pipe/PTY；取消、关闭、输入/输出超限和超时先向进程组发送`SIGTERM`，宽限到期后`SIGKILL`，根进程正常退出也清理残留后代。Linux额外在目标pre-exec设置parent-death signal并复核parent PID；该机制只缩小owner异常死亡窗口，不替代回执恢复。PTY通过独立master/slave、controlling terminal和有界resize/stdin命令工作；pipe保持stdout/stderr独立。

Windows pipe目标使用显式Win32 `CREATE_SUSPENDED → AssignProcessToJobObject → NtResumeProcess`，Job固定`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`且不允许breakaway；分配或恢复任一步失败都会终止仍挂起的目标。ConPTY使用`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE`与`PROC_THREAD_ATTRIBUTE_JOB_LIST`在同一次`CreateProcessW`中原子绑定终端和Job，`STARTUPINFOEXW`显式设置`STARTF_USESTDHANDLES`并将三个标准句柄置为`INVALID_HANDLE_VALUE`，由Pseudo Console建立终端端点，避免先运行再归属或继承宿主句柄。owner持有Job最后句柄，Agent宿主死亡使控制管道EOF并终止整棵树，owner自身异常退出也由kill-on-close清理。Windows stdin线程和输出线程均使用有界队列；ConPTY输入将LF规范为CR、退格规范为DEL，并保留跨分片CRLF语义。PTY关闭输入时发送`Ctrl+Z + CR`控制台EOF键并逻辑禁止后续输入，不提前关闭ConPTY传输句柄触发`CTRL_CLOSE_EVENT`；不解释EOF键的原始模式程序继续受deadline和停止控制约束。

双流在写盘前分别通过`StreamingSecretRedactor`，再按共享`ProcessSpec.output_bytes`预算计量。超过预算立即停止进程树，文件只保留获准前缀，同时完整记录脱敏后观察字节数、摘要、截断和EOF；读取时按Lease中的持久字节数和前缀摘要复核，替换、截短或伪造文件失败关闭。PTY为单一组合stdout流，stderr以空且EOF记录。控制帧最大1 MiB、单次stdin最大64 KiB、累计stdin受Plan预算约束；控制损坏、I/O故障或无法证明树清理进入`unknown/cleanup_failed`。

能力探测摘要绑定Python可执行文件身份、owner/协议/回执/平台实现模块及Windows受信Shell路径；每次spawn前重新计算，安装内容变化必须重新规划。Windows ConPTY只有在真实系统build和导出函数满足要求时广告，Job或启动属性在当前宿主不可用时启动失败，不回退到根PID或`taskkill`。本候选仍需远端Windows真机及macOS/Linux矩阵关闭实现差异，并在下一小节接入Container启动和立即网络再证明后才能关闭0.7.3。

### 12.3 Container统一Process生命周期

`ContainerExecutionSpec v1`把原`ContainerCommandSpec`、完整内层`ProcessSpec`和当前平台owner能力摘要组成一个自摘要合同。内层Process固定为相同argv的pipe模式，正式绑定process id、stdin、foreground/background、deadline、输入/输出预算；Container PTY在后端尚未形成三平台一致语义前拒绝规划。Execution Plan的Intent参数必须逐字段等于该合同，Sandbox继续绑定固定镜像、资源、网络与引擎能力，因此不能在批准后改变命令或生命周期限制。

`ContainerCommandBuilder`在执行时把内层命令确定性转换为Docker兼容argv：打开stdin时显式增加`--interactive`；增加由process id派生的唯一容器名，以及process id和execution digest双标签；Secret仍只以环境变量名进入argv。`ProcessLaunchBinding v1`再绑定Plan fingerprint、原始Intent参数摘要、外层ProcessSpec摘要、owner能力及物化环境摘要。通用Supervisor只接受该Container绑定后启动Docker客户端owner，Process Lease新增不可变launch binding摘要；Host调用仍要求Plan参数精确等于ProcessSpec，不能借预物化入口绕过。

选择性网络在全部Workspace、环境、Secret、Profile和argv检查完成后，立即执行固定`docker network inspect <name>`。当前证明必须再次满足internal bridge、Policy/Gateway标签和唯一`harnessix-egress`容器，并与Plan阶段`ManagedEgressBinding`完全相等；Podman选择性网络在等价inspect合同完成前失败关闭。引擎可执行文件在控制命令前后都复核对象身份。

Container实例使用`harnessix-process-<uuid>`名称和双标签核对生命周期。启动前固定`container ls --filter label=...`要求不存在同身份实例；自然退出、超时、取消、输出限制或调用方取消后，包装句柄以固定`container rm --force`清理残留并再次查询证明为空。启动半途失败也执行相同清理；宿主恢复通过Execution Plan中的ContainerExecutionSpec和Process Lease先核对owner回执，再执行幂等Container清理。查询失败、多个身份、名称或标签不匹配、删除失败或删除后仍存在均返回`process_cleanup_failed`，不得把内部Process Lease的退出误报为Container已完成。

该切片不把Docker Socket交给不可信工作负载，也不允许模型构造生命周期命令。Container生命周期调用只接受受信执行合同派生的固定argv，响应各限64 KiB并带时限。固定摘要BusyBox已经通过统一owner、只读Workspace、禁网、Secret流式脱敏、cgroup限制、tmpfs超限和无残留实例验证；Python 3.12/3.13、macOS、Windows、PostgreSQL和真实Container六矩阵均通过，0.7.3关闭。统一Tool入口仍属于0.7.5，不能由本节推导为所有旧工具已经迁移。

## 13. 0.7.4事务性交付与Git闭环详细设计

### 13.1 Workspace事务合同与私有CAS

`WorkspaceTransactionPlan v1`绑定transaction id、平台、来源`WorkspaceSnapshot`、有序文件Mutation、创建时间和自摘要。每个Mutation只允许普通文件或缺失状态，分别保存before/after的SHA-256、字节数和0644/0755模式；新增、修改、删除统一表示为同一路径的before/after状态，rename由内容相同且一删一增的两个Mutation在Diff层识别。路径按平台语义唯一排序，`.git`、`.harnessix`、`.codex`、`.agents`及Secret控制面永远拒绝写入。

Planner为每个目标同时捕获文件和全部现存父目录，避免只验证叶子内容而遗漏父目录替换；单文件不超过8 MiB、文件数不超过256、before与after镜像合计不超过32 MiB。公开Plan只含摘要，不携带正文。正文进入当前用户私有状态根的内容寻址存储，写入使用临时文件、`fsync`、原子replace和目录`fsync`，读取时重新核对大小、摘要、权限及对象类型。

`WorkspaceTransactionRecord v1`和append-only事件记录`prepared/publishing/interrupted/published/diverged/unknown`、相邻sequence和已确认游标。SQLite使用WAL、FULL同步、完整payload CAS和冗余列交叉校验。普通文件系统不承诺跨路径单系统调用原子性；产品承诺是任意中断后每个成员都可按before/after CAS归因，不能归因时停止为`diverged`或`unknown`。

### 13.2 发布、恢复与Rollback

发布必须持有跨进程Workspace fencing租约并核对批准fingerprint。首次写入前完整来源Snapshot必须相等；恢复时逐路径读取事实：全部before回到`prepared`，全部after进入`published`，before/after混合进入`interrupted`，出现第三种内容、链接、特殊文件或不可读状态进入`diverged/unknown`。执行器只对当前仍等于before的成员写入after；已经等于after的成员只推进账本，绝不重复写入。

POSIX写入通过root FD和逐段`openat`/no-follow父目录句柄定位；新内容先写同目录唯一临时普通文件，设置模式并`fsync`，再以`replace`或`unlink`提交，随后`fsync`父目录。每次成员事实核对后才推进持久游标；replace/unlink后、记账前崩溃由reconcile识别。Windows普通目录写入在具备抗Reparse Point竞态的句柄相对替换端口前失败关闭，Windows正式交付使用下一节的受管Git worktree。

Rollback不是倒退原记录或执行`reset --hard`，而是从已发布Plan生成新的反向事务：交换每个Mutation的before/after，以当前after事实作为新来源Snapshot，取得新的transaction id和批准。旧事务、旧批准和旧审计保持不可变；用户在发布后产生的第三种内容会使Rollback冲突而不是被删除。

### 13.3 完整Diff与Git Checkpoint

Diff必须列出所有路径、before/after摘要、模式和新增/修改/删除/重命名类别。UTF-8文本生成确定性unified diff；binary或超长单行记录完整文件摘要和字节数，不伪造文本patch。完整Diff正文作为私有Artifact保存并由摘要绑定Plan，模型只接收有界视图。

Git受管模式先绑定精确repository root、HEAD commit/tree、common directory、Git可执行文件身份和干净状态，再在私有根创建detached、no-checkout worktree。固定Git环境清除所有继承的仓库选择变量，关闭prompt、外部配置、replace refs、hooks、fsmonitor、外部attributes和可执行filter；submodule、LFS和稀疏checkout在0.7不支持时失败关闭。重开同时核对`.git`普通文件、common directory、管理回链和受管路径身份。

Checkpoint使用独立`GIT_INDEX_FILE`从基准tree开始，逐路径核对Git blob与Workspace before CAS，只加入Plan列出的最终路径并生成预期tree；随后以受管worktree自己的index执行固定`read-tree --reset -u`并重新核对tree和文件正文，不修改来源index。配置include、可执行filter、带转换规则的attributes、submodule、LFS、sparse checkout和alternates在0.7失败关闭。兼容基线包含macOS系统Git 2.24.3，不调用其尚未支持的`--path-format`、`--show-object-format`或`worktree list -z`。

Commit合同绑定此前不存在的新branch、parent、tree、作者、邮箱、带时区时间、消息、Hook策略、实现摘要、原始commit正文摘要和预期OID；默认Hook策略为disabled。规划阶段以不写对象的`hash-object`计算OID；执行阶段只写入同一确定性对象，再以全零旧值CAS创建branch ref。对象写入后或ref更新后崩溃分别恢复为`interrupted`或`committed`，对账同时核对OID、完整对象正文和ref，不根据返回码生成第二个commit。来源HEAD、来源index和现有branch均不移动；已存在branch、来源HEAD/配置/干净状态变化或非预期tree全部失败关闭。

### 13.4 Push边界

Push不属于本节本地事务的隐式尾步骤。`GitPushIntent`只描述remote名称、规范URL摘要、本地/远端ref、预期远端旧OID、force模式和幂等键，由0.7.5 Action Plane独立规划、批准、执行和reconcile。没有独立批准时不得建立网络连接；Commit成功、用户曾允许Git或仓库存在upstream均不能推导Push许可。

## 14. 0.7.5 Action Plane与安全验收详细设计

### 14.1 适用边界

0.7.5建立供内置Tool、后续MCP、Skill、Hook和custom adapter共同使用的唯一可信Action入口。0.8以前不开放第三方进程内Python插件；“扩展接入”在本阶段表示扩展只能持有受限能力端口，不能取得Host Executor、Session数据库、Secret Provider或Workspace文件对象。0.5历史Patch/Process事件与账本保持原样读取，不通过迁移伪造新审计；新接线只能把它们作为宿主adapter接入，不得删除其既有审批和恢复检查。

源码证据、调用链和独立取舍见[0.7.5统一Action Plane专项研究](research/unified-action-plane-and-extension-boundaries.md)与[ADR 0069](adr/0069-unified-coding-action-risk-route.md)。

### 14.2 领域合同

0.7.5新增以下公开v1合同：

- `CanonicalActionResource`：资源类型、访问方式、标识摘要和属性摘要；不保存路径、命令、URL或Secret明文；
- `TrustedToolBinding`：来源、source id、Tool版本/指纹、输入Schema摘要、宿主效果/风险、恢复模式、executor id和自摘要；
- `CodingActionInvocation`：调用身份、Tool身份、JSON参数和幂等键；刻意不允许调用方提交effect、risk、policy、Sandbox或executor；
- `ActionRoutePlan`：把Invocation、Binding、规范资源、`ExecutionPlanV2`、外部Action id和完整fingerprint冻结为一个不可变授权对象；
- `ActionAuditEvent`/`ActionRouteSnapshot`：append-only哈希链和当前状态投影；
- `ActionExecutionOutcome`：统一`succeeded/failed/unknown/manual_intervention`结果；
- `GitPushIntent`/`GitPushActionInput`/`GitPushReceipt`：证明外部非幂等写的独立合同。

所有合同使用`extra=forbid`、frozen、strict和禁NaN配置；跨JSON边界使用`model_validate_json`，避免严格UUID/时间合同在Python字典与真实JSON之间产生不同解析语义。生成Schema进入`spec/`并由测试与模型Schema逐项相等校验。

### 14.3 路由与策略

`TrustedActionRouter.register`只接受宿主构造的`TrustedActionDefinition`，注册时重新计算Pydantic输入Schema摘要。唯一键为`source/source_id/tool`；重复注册、Schema替换或执行前Binding变化均失败关闭。

规划顺序固定：

1. 重解析Invocation并精确匹配宿主Binding；
2. 拒绝参数树中的疑似明文凭据字段；
3. 用注册input model按真实JSON语义严格解析并规范化参数；
4. 由宿主resolver生成规范资源和Workspace Resource Request；
5. Policy只读取宿主Binding、规范资源、Sandbox和Secret bindings；
6. 捕获选择资源Workspace Snapshot并生成不可变`ExecutionPlanV2`；
7. 持久化Execution Plan，再保存Route Plan和初始审计事件。

默认Policy不信任模型风险自报：critical/destructive拒绝；read-only与写资源不一致、写工具无效果资源、network资源与Sandbox网络能力不一致、secret资源与Secret bindings不一致均拒绝；写入、medium/high、网络、Secret或外部reconcile要求批准；其余有界只读允许。

当前Execution Plan Store与Action Audit Store是两个私有SQLite文件。先写Execution Plan、后写Route Plan的崩溃最多产生不可达孤立Plan，不会产生可执行Route；执行必须同时重开两个对象并要求完全相等。清理孤立Plan属于后续存储维护，不改变安全性。

### 14.4 批准、执行与审计

Approval只绑定`plan_id + plan_fingerprint`，批准人只在审计中保存摘要。执行前重新检查：

- Route中的Execution Plan与Plan Store完全相等；
- 当前Registry Binding与持久Binding完全相等；
- Workspace根身份和全部选择资源Snapshot未变化；
- Approval Checkpoint对当前fingerprint仍有效；
- 持久参数仍能被同一Schema解析。

任一项失败时executor不会被调用。append-only审计事件保存状态、资源摘要、策略、批准摘要、executor id、输出/Artifact摘要、外部Action id、错误码和reconcile结论，不保存输出或文件正文。为执行与恢复保留的私有Route Plan payload包含规范化调用参数，因此可能包含路径、命令参数等非Secret元数据；疑似凭据字段在规划时拒绝，Secret值只能经独立Secret Provider短期解析。两个SQLite文件都必须按敏感运行状态保护，不得把事件“只含摘要”误写为整个数据库不含调用参数。SQLite使用WAL、FULL同步、不可变plan payload、冗余索引交叉校验和相邻哈希链；同UID恶意进程仍属于本地宿主信任边界，摘要不是数字签名。

状态机为：

```text
deny → denied
require_approval → pending_approval → ready | denied
allow → ready
ready → running → succeeded | failed | unknown
unknown → reconciling → succeeded | failed | unknown | manual_intervention
unknown → manual_intervention（无恢复能力）
```

读执行异常可确定为failed；写执行未分类异常保守为unknown。重启扫描发现`running/reconciling`时只转unknown，不调用execute。reconcile只能从unknown进入；外部Action identity不一致或对账异常回到unknown，避免卡在reconciling或误报终态。

### 14.5 Extension强制端口

`ExtensionActionPort`创建时固定`source`和`source_id`，只提供本来源Binding查询、plan、execute、reconcile和status。它没有公共executor、Session、Secret或文件系统属性；读取或执行其他来源Plan返回`extension_plan_denied`。扩展提交的Tool版本、指纹或参数即使语法合法，也必须与宿主Binding和Schema精确一致。

该边界是能力最小化，不是把任意恶意Python代码放进同一进程后的OS隔离。未来MCP/Skill/Hook进程生命周期由0.8管理；不受信代码仍必须进入0.7已交付的Process/Sandbox端口。

### 14.6 Git Push外部副作用

Push与Commit严格分离。`GitPushActionExecutor.prepare_intent`只读取本地仓库、local ref和remote配置，不连接远端；expected remote OID必须来自此前获准的观察或明确的“ref必须不存在”前提。Intent绑定：

- 精确`GitRepositoryBinding.digest`；
- remote名称和去凭据、去歧义后的URL摘要；
- 一个local branch ref/OID和一个remote branch ref；
- expected remote OID；
- `fast_forward_only`或显式`force_with_lease`；
- 幂等键和自摘要。

执行只在统一Route进入running且Approval有效后，确定性投影到既有Effect Journal。旧ActionService另有`ApprovedGitPushPolicy`，直接调用、Route未批准、Route不在running、plan/intent/action id任一错绑均拒绝。Git命令固定`--no-verify`和单ref refspec，禁prompt、外部配置、Hook、replace refs、外部attributes，协议限制为显式file/https/ssh集合；file只用于测试或宿主显式开启。

Push前重新核对仓库、remote URL、local OID和远端lease；`fast_forward_only`还验证旧远端OID是local OID祖先。Push调用一旦开始，启动/等待/返回/后续观察的任何异常都进入unknown。对账只执行`ls-remote`：目标OID表示成功，旧OID表示未应用，第三种OID表示人工处置；绝不再次Push。

0.7受控真实场景使用本地bare remote，不需要凭据且不发生公网访问。HTTPS/SSH凭据、SSH Agent/known-hosts和平台Keychain必须由0.8.6的Secret/配置产品化显式装配；当前Runner不继承完整宿主环境，也不允许URL凭据或Token进入Intent/argv。

### 14.7 文件、命令和外部效果审计

Action Audit事件不保存敏感正文：Workspace资源记录路径标识摘要和read/write/execute；Process资源记录程序/参数合同摘要和execute；Network记录规范目标摘要；Secret只记录名称/版本绑定摘要；Git ref和外部资源记录remote/ref/lease摘要。不可变Route Plan另行保存执行所需的规范化参数，但不保存Secret值。具体Process与文件执行仍由0.7.1～0.7.4各自的Snapshot、Lease、Artifact和Delivery Ledger保存可恢复事实，Action Audit只提供跨组件统一因果索引，不复制第二份效果真相。

### 14.8 验证与当前限制

候选门禁覆盖：五类Tool来源同策略、未注册/伪造风险/Schema替换、明文凭据、扩展跨来源访问、审批和Workspace漂移、审计payload/index/事件链损坏、真实宿主`os._exit`恢复、真实Git仓库/受管worktree/checkpoint/commit/bare remote Push、直接ActionService旁路、remote/ref命令参数注入、LF/CRLF输出边界、remote配置漂移、Push返回丢失和只对账不重放。Trusted Action与Git Push专项共45项，Delivery回归52项；Windows Snapshot加固后全仓本地门禁为3139 passed、12 skipped。

0.7.5不交付MCP协议客户端、Skill加载器、Hook进程、CLI审批UI、远端凭据产品化或多租户签名审计；这些是0.8/0.9工作。0.7实现及Windows Snapshot稳定性加固已通过[CI 34265610488](https://github.com/carrie1988/Harnessix/actions/runs/34265610488)的Python 3.12/3.13、macOS、Windows、PostgreSQL和固定摘要真实Container六矩阵，0.7全部切片据此关闭。
