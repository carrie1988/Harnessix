# Harnessix Code 0.7 可信执行与工程交付设计

- 状态：实施中
- 更新日期：2026-09-08
- 适用范围：0.7.0～0.7.5

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
