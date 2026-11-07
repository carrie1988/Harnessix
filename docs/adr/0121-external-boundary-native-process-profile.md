---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [product_config, processes, sandbox, evals]
related_adrs:
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
related_tests:
  - tests/product_config/test_native_process_profile.py
  - tests/product_config/test_native_process_action.py
supersedes: []
---

# ADR 0121 外部边界原生进程Profile

## 1. 背景

- 产品执行任意命令的工具（`exec_command.<id>`）只有容器Process Profile一条路，必须指定Docker或Podman。
- Harbor评测时，Harnessix运行在SWE-bench任务容器内，容器里没有容器引擎，Harnessix因此没有任何命令工具。
- 原计划（原生进程沙箱草案）是在任务容器内用Landlock+seccomp执行命令。2026-11-07调研业界做法：
  - Codex、Claude Code、Cursor、Gemini CLI在沙箱可用时都允许命令写工作区并保留改动；没有产品在每条命令结束后自动撤销写入。
  - 评测容器内，主流智能体都关闭自身沙箱，以任务容器为边界。Codex文档明确建议在Docker中用`--sandbox danger-full-access`；
    Harbor 0.25内置的Claude Code、Codex、OpenCode、Cursor等适配器全部关闭自身沙箱或审批。
  - Codex已把Landlock降为旧选项，原因是它无法隔离Unix套接字；没有产品在不能建命名空间的Docker里提供Landlock+seccomp沙箱。

## 2. 用户决策（2026-11-07）

1. 评测中命令可以写工作区，改动保留（对应原生沙箱草案ADR 0112第5节的方案C）。
2. 原生沙箱范围调整：
   - 产品原生沙箱仍是产品能力，按业界做法实现macOS Seatbelt与Linux宿主机bubblewrap。
   - 容器内的Landlock+seccomp后端改为可选，往后排。
   - Harbor评测以任务容器为边界：Harnessix不另加沙箱，命令由适配器自动批准。
3. 实现方式：新增外部边界原生进程Profile（本ADR）。

原生沙箱草案（ADR 0111、0112，位于沙箱分支，尚未合入）需要按第2条改写；改写由沙箱负责方完成，本ADR不修改草案。

## 3. 决策

### 3.1 配置：独立的原生进程Profile

- 新增`harnessix.product-native-process-profile/v1`，与容器Profile并列，不给容器Profile加可选字段（与ADR 0111草案第2.7节一致）。
- 挂在`ProductActionConfigV1.native_process_profiles`下；为空时不序列化，已有Action配置的摘要不变。
- 本期字段：
  - `profile_id`、`version`、`description`；
  - `backend`：本期只有`external_boundary`，Seatbelt、bubblewrap等后端后续加入同一字段；
  - `program`（宿主绝对路径）与`arguments`，模型提交的`cmd`作为最后一个参数；
  - `workspace_mode`：本期只有`read_write`，命令直接读写工作区，改动保留；
  - `timeout_seconds`、`max_output_bytes`；
  - `environment`：宿主固定的非Secret环境预设，进入Profile摘要；
  - `profile_sha256`。
- 原生Profile与容器Profile共用命令类身份：工具名`exec_command.<profile_id>`、执行器`product.command-profile.<profile_id>`，ID不得重复。
  公开输出合同与启动恢复（活跃进程归属核对）按这一身份识别命令；两类Profile的工具版本与指纹不同，Binding仍各自独立。

### 3.2 外部边界的启用条件

- 只有同时满足以下两条才装配命令工具，任何一条不满足都省略该工具并给出原因码，绝不降级为其他执行方式：
  - 配置显式声明`backend: external_boundary`；
  - App Server进程环境中`HARNESSIX_EXTERNAL_BOUNDARY=1`，由启动容器的一方断言自己提供了隔离边界（与Claude Code在容器内要求`IS_SANDBOX=1`同理）。
- 只支持POSIX平台的Process Owner；Windows报`process_owner_unavailable`。

### 3.3 执行与证据

- 执行计划如实记录：`SandboxBinding`为`host_guarded`、后端`host`、网络`full`；能力证据的提供方为`external-boundary`。
  这样外部边界的运行不会被误认为`host_sandboxed`或`container_strong`。
- Action资源声明工作区写权限（`mount: external_boundary`）和网络连接（`enforcement: external_boundary`）。
- 命令以工作区根为当前目录，只得到Profile预设的环境变量，不继承App Server的环境（模型凭据等不经环境变量泄给命令）。
- 进程监管、超时、输出上限、账本恢复复用现有POSIX Process Owner；执行器与Eval测试Profile共用同一宿主执行器实现。
- Harnessix不提供文件系统和网络隔离：命令能读到同一边界内的文件（含Harnessix状态目录），网络由外部边界决定。

### 3.4 审批

- 工具名前缀仍是`exec_command.`，`exec --approval-mode auto`与Harbor适配器沿用现有规则自动批准，审批策略不改。
- 审批记录照常写入，收据中的自动批准次数可审计。

## 4. 取舍

- **不在产品内做隔离。** 换来在任何容器里都能用，代价是安全性完全依赖外部边界；因此只能显式开启，并要求环境断言。
- **网络记为`full`。** Harnessix不知道外部边界的网络策略，只记录“未由Harnessix限制”，不宣称更强的保证。
- **工作区可写。** 与主流智能体和Harbor其他参评对象一致；与一期247上“命令在一次性副本中运行”的数据不能直接对比。

## 5. 失败语义

| 原因码 | 含义 |
|---|---|
| `native_backend_unsupported` | 声明的后端本期未实现 |
| `external_boundary_unasserted` | 缺少`HARNESSIX_EXTERNAL_BOUNDARY=1`断言 |
| `process_owner_unavailable` | 当前平台没有可用的POSIX Process Owner |

命令执行期间的失败沿用宿主执行器的现有原因码（超时、输出超限、启动失败、状态未知等）。

## 6. 验收

- 合同：摘要稳定、字段越界拒绝、与容器Profile ID冲突拒绝、空列表不改变旧配置摘要。
- 装配：缺少环境断言时工具被省略且原因码正确；断言存在时工具可用，执行计划记录`host_guarded/host/full`。
- 执行：命令在工作区执行并保留写入；不继承App Server环境；超时与输出上限生效；重启后按账本对账，不重放命令。
- 评测：E0的Harnessix臂在Harbor任务容器内使用本Profile完成5题，官方判分与Harbor奖励一致，逐步记录可从Langfuse读回。
