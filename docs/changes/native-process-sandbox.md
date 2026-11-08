---
doc_type: change-design
status: reviewing
version: 1
code_revision: pending
owners:
  - core
modules:
  - sandbox
  - execution
  - product_config
related_adrs:
  - docs/adr/0111-native-process-sandbox-backends.md
  - docs/adr/0112-native-workspace-modes.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
  - docs/adr/0121-external-boundary-native-process-profile.md
related_tests:
  - tests/sandbox/test_native_policy.py
  - tests/sandbox/test_native_probe.py
  - tests/product_config/test_native_process_profile.py
  - tests/execution/test_plans.py
  - tests/sandbox/test_bubblewrap.py
  - tests/sandbox/test_seatbelt.py
  - tests/product_config/test_native_sandbox_profile.py
  - tests/product_config/test_native_sandbox_action.py
  - tests/product_config/test_native_host_command.py
  - tests/product_config/test_zero_config.py
  - tests/trusted_actions/test_sandboxed_command_policy.py
supersedes: []
---

# 原生进程沙箱变更设计

本文按实施步骤记录原生进程沙箱实际落地的合同、兼容性与验证证据。决策理由见以下 ADR，本文不重复：
- [ADR 0111](../adr/0111-native-process-sandbox-backends.md)：后端与边界；
- [ADR 0112](../adr/0112-native-workspace-modes.md)：工作区模式；
- [ADR 0113](../adr/0113-sandboxed-command-approval-and-host-escalation.md)：审批与沙箱外命令。

## 1. 需求背景

- 产品的命令工具只有两条路：容器 Profile（必须装 Docker 或 Podman），或外部边界 Profile（Harnessix 不加隔离）。
- 没装容器引擎的开发机没有“带隔离的命令执行”。
- 配置命令工具要手写带摘要的 Action 配置，普通用户无法完成。
- 用户要求（2026-11-07）：面向个人用户，零配置、开箱即用；沙箱内命令自动执行，越界才询问。

## 2. 设计目标

1. macOS 用 Seatbelt、Linux 宿主机用 bubblewrap 执行命令。执行计划如实记录后端与实际生效的保证项。
2. 不写配置即可使用；沙箱不可用时明示，并改为逐条批准的沙箱外命令。
3. 不新增依赖，复用现有 POSIX Process Owner、宿主执行器与 Action 路由。
4. 已有配置、执行计划和外部边界 Profile 的摘要与行为保持不变。

## 3. 实施步骤与状态

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | 探测 v2 与合同：Profile 字段、`SandboxBinding` 规则、沙箱策略推导、环境变量白名单、保证清单 | 已完成 |
| 2 | bubblewrap 参数翻译与执行，247 宿主机验证 | 已完成；247 以 `nobody` 身份全部通过（第 12 节） |
| 3 | Seatbelt 参数翻译与执行（含已配置代理端口拦截） | 已完成；真实 Seatbelt 端到端 5 项在用户普通终端全部通过（第 12 节） |
| 4 | 审批策略 v2、沙箱外命令、零配置装配、诊断 | 已完成（第 16 节）；用户可见的启动提示待对齐 |
| 5 | 三环境验收；回写 ADR 0066 第 2 版与模块设计 | 已完成；验收见第 12 节，2026-11-08 合入主线时写回 ADR 0066 第 2 版，ADR 0111–0113 转为已接受 |

## 4. 总体架构

| 层 | 职责 | 源码 |
|---|---|---|
| 探测 | 判断本机后端是否可用，给出身份、原因码与保证项 | `sandbox/native_probe.py` |
| 策略 | 从本机事实推导一个工作区的完整边界与命令环境，平台无关 | `sandbox/native_policy.py`、`sandbox/native_contracts.py` |
| 翻译 | 把策略翻译成 `bwrap` 参数，或 Seatbelt 策略文本加 `-D` 参数 | `sandbox/bubblewrap.py`、`sandbox/seatbelt.py` |
| 证明与启动准备 | 探测、推导策略、绑定执行计划；启动前复核后端身份、清空私有 TMPDIR、查找 `.env` | `product_config/native_sandbox.py` |
| 装配与执行 | 显式配置的 bubblewrap Profile 已接入；零配置在第 4 步 | `product_config/native_process.py`、`host_process_action.py` |
| 审批 | 策略把沙箱内命令标记为可由系统批准，宿主以系统身份当场批准；沙箱外命令逐条人工批准 | `trusted_actions/policy.py`、`agent/runtime.py`、`product_config/sandbox_approval.py` |

策略层只做确定性推导，不读写文件系统；采集本机事实与执行命令都放在边缘，便于测试和审计。

## 5. 流程与数据流

1. 启动时探测后端，得到 `HostSandboxProbeV2`。
2. 采集本机事实：HOME、工作区、Git 目录、Harnessix 状态与配置路径、代理端口、私有目录。
3. 由本机事实推导 `NativeSandboxPolicy`，它的摘要计入执行计划的 `SandboxBindingV2.profile_digest`。
4. 每条命令启动前：复核后端可执行文件身份，清空私有 TMPDIR，查找工作区内的 `.env` 文件，生成完整 argv，然后由 POSIX Process Owner 监管执行。

bubblewrap 的挂载顺序决定覆盖关系，后挂载的覆盖先挂载的：
1. `--ro-bind / /`；
2. 新的 `/dev`、`/proc`；
3. 私有 tmpfs 覆盖 `/tmp`、`/run`；
4. 可写目录 `--bind`；
5. `.git` 以 `--ro-bind` 覆盖为只读；
6. 拒读目录用空只读目录覆盖，拒读文件与 `.env` 用 `/dev/null` 覆盖；
7. `--chdir` 到工作区。

## 6. 接口设计

| 函数 | 输入 → 输出 |
|---|---|
| `probe_native_sandbox(platform=…, runner=…, nested=…)` | 平台与可替换的执行器 → `HostSandboxProbeV2` |
| `build_native_sandbox_policy(facts)` | `NativeSandboxFacts` → `NativeSandboxPolicy`；冲突时报 `KernelError` |
| `workspace_git_directories(workspace)` | 工作区 → Git 目录元组（含 worktree 的 gitdir 与 common dir） |
| `loopback_proxy_ports(urls)`、`environment_proxy_urls`、`macos_system_proxy_urls` | 代理设置 → 本机回环代理端口 |
| `sandbox_environment(...)`、`host_environment(...)` | 启动环境、私有目录、预设 → 沙箱内或沙箱外命令的环境 |
| `sandbox_reserved_environment_problem(name)` | 变量名 → 是否由 Harnessix 管理（Profile 预设不得覆盖） |
| `bubblewrap_argv(policy, executable=…, command=…, masks=…, env_files=…, hidden_roots=…)` | 策略与遮盖对象 → 完整 argv；超出 128 项时报 `sandbox_policy_too_large` 或 `workspace_secret_files_unmaskable` |
| `workspace_env_files(workspace, git=…)` | 工作区 → 需遮盖的 `.env` 文件 |
| `seatbelt_policy(policy)`、`seatbelt_argv(policy, command=…)` | 策略 → Seatbelt 策略文本与 `-D` 参数、完整 argv |
| `configured_proxy_ports(environ, runner)` | App Server 环境与 `scutil --proxy` → 需拦截的本机代理端口 |
| `attest_native_process_profile(…, site=…, secret_names=…, sandbox_probe=…)` | 原生 Profile 与宿主位置 → 原因码与证明；沙箱后端新增 `site` 参数 |

## 7. 数据结构与领域契约

| 合同 | 要点 |
|---|---|
| `harnessix.host-sandbox-probe/v2` | 后端必须与平台对应；可用时必须带完整身份、原因码 `verified` 与该后端的保证清单；不可用时不得声明保证项；自摘要。v1 合同与 Schema 不变 |
| `harnessix.native-sandbox-policy/v1` | 路径为规范绝对路径，集合唯一有序；可写目录必须包含私有临时目录、私有缓存目录，以及 `read_write` 模式下的工作区；可写目录不能是根目录、HOME 或其上级，也不能位于拒读路径之内；bubblewrap 不得携带拦截端口；自摘要 |
| `ProductNativeProcessProfile` | `backend` 增加 `seatbelt`、`bubblewrap`、`host`；`workspace_mode` 增加 `read_only`；新增 `write_roots`（最多 8 项）与 `allow_workspace_env_files`，取默认值时不序列化 |
| `SandboxBinding` | `host_sandboxed` 只能使用 `seatbelt` 或 `bubblewrap` |

## 8. 失败、恢复与超时

- 探测中的任何异常都折算为不可用原因码，不抛到启动流程；预检有 10 秒超时。
- 推导出的策略不合法时报 `sandbox_root_overlaps_denied` 或 `sandbox_path_invalid`，命令工具失败关闭。
- 新后端接入执行器之前，`attest_native_process_profile` 报 `native_backend_unsupported` 并省略工具，绝不按外部边界执行。
- 执行期的超时、取消、重启对账沿用 POSIX Process Owner。
- 启动准备失败时命令不执行，公开结果为 `failed`、`process_preflight_failed`（沿用现有公开错误码，不扩大公开合同）。这类失败包括：后端身份漂移、私有目录无法准备、参数超限、`.env` 无法完整遮盖。
  - 为此，共享宿主执行器 `HostProcessActionExecutor` 把派生 `ProcessSpec` 的调用移进与启动相同的失败判定。
  - 之前这类异常会被记为“效果未知”。
- Seatbelt 在证明阶段用将要执行的同一份策略运行一次 `/usr/bin/true`。系统拒绝策略时报 `seatbelt_preflight_failed`，整体省略工具，避免每条命令都失败。
- bubblewrap 在执行命令前失败时，报错进入命令输出，结果记为非零退出。是否用 `--json-status-fd` 区分“后端失败”与“命令失败”，在第 5 步验收时决定。

## 9. 持久化、事务与数据流程

- 不新增持久化。策略摘要进入执行计划后，随现有计划与路由账本持久化。
- 私有目录在每条命令启动前按需创建，位于用户缓存目录，不在 Harnessix 状态库内：
  - Linux 为 `${XDG_CACHE_HOME:-~/.cache}/harnessix/sandbox/<工作区摘要>/`，macOS 为 `~/Library/Caches/harnessix/sandbox/<工作区摘要>/`；
  - `tmp/` 每条命令前清空，`cache/` 跨会话保留，`cache/.empty` 用作拒读目录的遮盖源。
- 私有目录路径按工作区固定、不随启动变化，因此重启后旧计划仍能按原绑定对账。产品按状态目录持有单一运行锁，同一工作区不会有两个 App Server 同时使用它。

## 10. 安全、权限与信任边界

- 拒读清单涵盖：凭据、命令历史、浏览器与钥匙串、Harnessix 状态与配置、工作区 `.harnessix`。
- 可写目录与拒读清单冲突时失败关闭。
- 命令环境只继承白名单内的变量，模型凭据与 `HARNESSIX_*` 永不继承；代理变量只给沙箱外命令。
- 可执行文件只查固定路径，不受 PATH 劫持。
- Seatbelt 策略文本只由代码生成的固定片段和参数名组成，路径一律经 `-D` 参数传入，路径中的任何字符都不能改变策略结构。端口是经合同校验的整数。
- Seatbelt 规则要点：
  - 默认拒绝；整机可读，拒读清单覆盖；只放行可写目录和 `/dev/null` 等设备。
  - 信号与进程信息只限同一沙箱。
  - Mach 服务只放行名字解析、偏好、日志、临时目录查询等最小清单，不含钥匙串与 LaunchServices。
  - 网络放行宿主回环，再单独拒绝已配置的代理端口；Unix 套接字只限私有 TMPDIR。
  - `.env` 规则只对普通文件生效。
- 工作区 `.env` 判定与读工具一致（`.env` 或 `.env.*`，大小写不敏感），但只遮盖文件。名为 `.env` 的虚拟环境目录不遮盖，否则会破坏常见的 Python 项目。
- 查找 `.env` 时调用宿主 Git：
  - 固定忽略全局与系统配置，关闭 fsmonitor 与可选锁；
  - 只运行 `ls-files --cached` 与 `status --ignored=matching --untracked-files=all`，不执行钩子；
  - 整体被忽略的目录（如 `node_modules/`）只列一项，不深入其内部。

## 11. 可观测与错误分类

| 原因码 | 含义 |
|---|---|
| `verified` | 后端可用 |
| `native_backend_platform_mismatch` | 当前平台没有对应后端 |
| `seatbelt_not_installed`、`bubblewrap_not_installed` | 未找到后端可执行文件 |
| `seatbelt_nested_unavailable` | 当前进程已在 Seatbelt 中，无法嵌套 |
| `seatbelt_preflight_failed`、`bubblewrap_preflight_failed` | 预检失败 |
| `bubblewrap_namespaces_denied` | 系统不允许创建所需命名空间 |
| `sandbox_root_overlaps_denied`、`sandbox_path_invalid` | 策略推导失败 |
| `native_backend_unsupported` | 后端尚未接入执行器（当前：Seatbelt、`host`） |
| `native_sandbox_site_unavailable` | 调用方没有提供工作区位置（例如诊断时未指定工作区） |
| `sandbox_backend_changed`、`sandbox_policy_too_large`、`workspace_secret_files_unmaskable` | 启动准备失败；公开结果为 `process_preflight_failed` |
| `seatbelt_preflight_failed`（证明阶段） | 系统拒绝生成的 Seatbelt 策略；省略工具 |

## 12. 测试与验证

- 新增及修改的测试共 69 项，全部通过：
  - [`test_native_policy.py`](../../tests/sandbox/test_native_policy.py)
  - [`test_native_probe.py`](../../tests/sandbox/test_native_probe.py)
  - [`test_native_process_profile.py`](../../tests/product_config/test_native_process_profile.py)
  - [`test_plans.py`](../../tests/execution/test_plans.py)
- 回归范围：`tests/sandbox`、`tests/execution`、`tests/trusted_actions`、`tests/product_config`、`tests/governance`、`tests/test_exec_cli.py`。
  - 与同一基线提交的干净 worktree 对照，失败与错误集合完全相同（各 744 项，都是会话沙箱环境造成的既有失败）。
  - 本变更多出 47 项通过。
- 本机实测：
  - `sandbox_check` 在本会话中返回 1；
  - 嵌套运行 `sandbox-exec` 退出码为 71，报 `sandbox_apply: Operation not permitted`。
- 247 实测（2026-11-07，以 `setpriv` 降权为 `nobody`，bubblewrap 0.6.1）：
  - 手工按第 5 节的挂载顺序运行：`~/.ssh` 被遮成空目录，工作区 `.env` 读写都被拒；工作区可写，`.git` 与 `/etc` 只读；`/tmp` 私有可写，`/run` 为空；
  - 回环可用，外网连接返回 ENETUNREACH；PID 命名空间内只有 4 个进程；Python 多进程锁正常。
  - 在 `python:3.11-slim` 临时容器中，root 与 `nobody` 运行 bubblewrap 都报 `No permissions to create new namespace…`，分类为 `bubblewrap_namespaces_denied`；容器以 `--rm` 运行，用完已删除。
- 第 2 步测试：
  - [`test_bubblewrap.py`](../../tests/sandbox/test_bubblewrap.py)：参数翻译与顺序、超限、遮盖对象、私有目录、`.env` 查找；
  - [`test_native_sandbox_profile.py`](../../tests/product_config/test_native_sandbox_profile.py)：证明、计划绑定、资源声明、身份漂移；
  - [`test_seatbelt.py`](../../tests/sandbox/test_seatbelt.py)：策略结构、参数化（路径不进入策略文本）、规则顺序、参数上限；
  - [`test_native_sandbox_action.py`](../../tests/product_config/test_native_sandbox_action.py)：真实后端端到端，Linux 跑 bubblewrap、macOS 跑 Seatbelt，按后端核对网络边界。另有一项直接运行生成的策略，策略被系统拒绝时给出后端原始报错。
  - 本会话本身运行在 Seatbelt 中，嵌套被系统拒绝，这些用例自动跳过（原因码 `seatbelt_nested_unavailable`）。需由用户在普通终端运行。
- macOS 27 真实 Seatbelt 验收（2026-11-07，用户普通终端，共三轮）：
  - **第一轮**：第 5 项通过，说明生成的策略被系统接受。其余 4 项在启动前失败：沙箱计划的能力证据摘要不等于 POSIX Owner 能力摘要，被启动绑定拒绝。修复后补了透传后端的全链路测试。
  - **第二轮**：4 项通过，代理端口未被拦截。
    - 对照实验表明 SBPL 以最后一条匹配的规则为准：同一端口“先放行后拒绝”时被拦，“先拒绝后放行”时放行。
    - 生产策略里，端口拒绝规则之后还有 Unix 套接字的网络放行规则，覆盖了拒绝。
    - 修复：全部放行规则在前、全部拒绝规则在后，并由单元测试守住这一顺序。
  - **第三轮**：确认实验显示，旧顺序下代理端口可连，新顺序下被拦截。两种顺序下，其他本地端口与私有 TMPDIR 内的 Unix 套接字都可连，目录外的 Unix 套接字与外网都被拦截。端到端 5 项全部通过。
  - 本机：除需要真实 bubblewrap 的 4 项外全部通过；这 4 项待在 247 以 `nobody` 身份运行（`--basetemp` 设在 `/data/harnessix/sandbox-dev/` 下，避免工作区落在被私有 tmpfs 覆盖的 `/tmp`）。

## 13. 源码映射

| 关注点 | 源码 |
|---|---|
| 探测与策略合同 | [`native_contracts.py`](../../src/harnessix/sandbox/native_contracts.py) |
| 策略推导、代理端口、环境白名单 | [`native_policy.py`](../../src/harnessix/sandbox/native_policy.py) |
| 后端探测 | [`native_probe.py`](../../src/harnessix/sandbox/native_probe.py) |
| Profile 合同 | [`action_contracts.py`](../../src/harnessix/product_config/action_contracts.py) |
| 外部边界证明的后端守卫 | [`native_process.py`](../../src/harnessix/product_config/native_process.py) |
| `SandboxBinding` 规则 | [`execution/contracts.py`](../../src/harnessix/execution/contracts.py) |
| bubblewrap 翻译、遮盖与 `.env` 查找 | [`bubblewrap.py`](../../src/harnessix/sandbox/bubblewrap.py) |
| 沙箱证明与启动准备 | [`native_sandbox.py`](../../src/harnessix/product_config/native_sandbox.py) |
| 启动准备失败的效果判定 | [`host_process_action.py`](../../src/harnessix/product_config/host_process_action.py) |
| Seatbelt 策略翻译 | [`seatbelt.py`](../../src/harnessix/sandbox/seatbelt.py) |

## 14. 部署、兼容与回退

- **兼容**：
  - 新字段取默认值时不序列化，已有 Action 配置与外部边界 Profile 的摘要不变。
  - 新增 `spec/host-sandbox-probe-v2.schema.json` 与 `spec/native-sandbox-policy-v1.schema.json`；Action 配置相关 Schema 由生成脚本更新。
- **回退**：第 1 步不改变任何运行时行为。新后端在配置中出现时一律被省略。回退只需还原本步提交。

## 15. 风险与取舍

- 拒读清单需要持续维护；清单之外的文件可被读取（ADR 0111 第 4 节）。
- bubblewrap 预检的报错分类依赖报错文本；已用真实报错验证。
- 参数数量受 `ProcessSpec` 128 项上限约束：固定部分约 40 项，每个可写目录、遮盖对象各占 3 项。拒读清单只遮盖实际存在的路径，常见机器余量充足；工作区 `.env` 超过余量时拒绝执行。
- 工作区位于 `/tmp` 之下时，可写目录在私有 tmpfs 之后挂载，仍然可见。但位于 `/tmp` 之下的 HOME 等其他路径会被私有 tmpfs 遮住。
- 非 Git 工作区的 `.env` 查找跳过常见依赖与构建目录、只查 8 层以内，更深处的 `.env` 不保证遮盖，如实声明。
- Seatbelt 以最后匹配为准：新增任何放行规则都必须排在拒绝规则之前，否则会悄悄覆盖拒绝。单元测试 `test_every_deny_rule_comes_after_every_allow_rule` 守住这一点。
- Mach 服务清单目前只覆盖已验收的命令（shell、git、python）。其他工具若需要更多系统服务，会表现为命令失败，按反馈补充。

## 16. 第 4 步：审批、沙箱外命令与零配置

### 16.1 沙箱内命令的自动批准

- **策略**：`trusted-coding-action/v2` 只新增一条标记规则：沙箱内命令判为“需要审批”，`policy_id` 为 `default.approve-sandboxed-command`。
- **为什么不是直接放行**：Agent 内核要求写调用成功前必有已批准的审批项。实测直接放行会让输出发布判为“缺少批准”，整轮中断。
- **宿主钩子**：`AgentRuntime(approval_responder=…)`。在三个入口的最外层、不持锁时，经公开的 `reply_approval` 落盘系统决定后继续执行；`resume_turn` 进入时先补批。
- **应答器**：`sandbox_approval.py` 按 Router 持久计划复核策略 ID、`host_sandboxed`、后端、计划指纹和路由状态，批准人为 `harnessix:sandbox-policy`。
- **客户端晚到的回复**：已由系统身份决定时，结论相同的回复幂等成功、审批只记一次；结论相反仍报 `approval_conflict`。这一点由评测会话提出：Harbor 的协议客户端处于 auto 模式时会遇到。
- **方案选择**：钩子放在运行时而非 App Server 驱动，与评测会话商定。放在运行时客户端不会长时间看到待审批；评测会话不改运行时的审批段。

### 16.2 沙箱外命令

- **身份**：`host_command.<id>` ↔ `product.host-command.<id>`，公开输出合同与启动恢复都已登记这一配对。
- **参数**：`cmd` 加必填单行 `reason`（最多 500 字节）。
- **执行计划**：`host_guarded/host/full`；环境继承白名单并额外继承代理变量，不含模型凭据与 `HARNESSIX_*`。
- **审批**：策略按 v1 规则要求人工批准，应答器不认可，`exec --approval-mode auto` 的前缀自动批准也不覆盖它。

### 16.3 零配置

- `harnessix code`、`doctor` 和 `exec` 在没有 Action 配置时，在预检请求里置 `zero_config_actions`，并给 App Server 传 `--zero-config-actions`。
  - 最初的实现是设置进程环境变量。全量回归发现它会在同一进程内的后续测试中残留，导致两项无关测试失败，于是改为显式参数，不修改环境。
- `zero_config.py` 生成命令工具：
  - 沙箱可用：`exec_command.shell` 加 `host_command.host-shell`；
  - 沙箱不可用：只有后者，说明中写明原因；
  - shell 取登录 shell（zsh 或 bash），否则 `/bin/sh`。
- 评测与程序化启动不设置这一请求，省略 Action 配置时仍得到原内建配置。测试守护“原内建配置逐字不变”。
- 存在本机命令工具时，产品使用通用工具视图与新的本机命令系统指令；外部边界与容器的指令不变。

### 16.4 验证

- 本机通过：
  - 策略 v2（9 项）；
  - 沙箱外命令（9 项）；
  - 零配置（6 项）；
  - 透传后端的全链路自动批准与崩溃恢复补批；
  - 系统决定后的重复回复幂等。
- 运行时钩子改动后，`tests/agent`、`tests/app_server`、`tests/test_exec_cli.py`、`tests/trusted_actions`、`tests/product_config` 与干净基线对照，失败集合完全相同。
- 第 4 步全部改动后，再扩大到 `tests/product_ui`、`tests/execution`、`tests/sandbox`、`tests/governance` 与基线对照。
  - 第一次发现 2 项新失败，都是零配置请求写入进程环境造成的测试串扰，已改为显式参数。
  - 第二次全量回归曾卡在 App Server 的 stdio 测试附近；该文件单独运行 42 项全部通过，第三次全量回归也正常结束，判定为偶发，与本变更无关。第三次结果与基线失败集合相同。
- 247（bubblewrap 0.6.1，以 `nobody` 身份）：第 2 步代码加策略 v2 后，沙箱相关 218 项全部通过，含 4 项真实 bubblewrap 端到端。
- 第 5 步验收（提交 `6daa8e05`）：
  - 本机全量回归（`tests/agent`、`tests/app_server`、`tests/product_ui`、`tests/product_config`、`tests/trusted_actions`、`tests/execution`、`tests/sandbox`、`tests/governance`、`tests/test_exec_cli.py`）与干净基线失败集合相同，无新增失败。
  - 247 以 `nobody` 身份：沙箱相关加 App Server 共 282 项全部通过，无跳过，真实 bubblewrap 端到端断言批准人为 `harnessix:sandbox-policy`；`harnessix code doctor` 显示 `[通过] action/product_native_sandbox: native_sandbox_bubblewrap`。
  - macOS（用户在系统“终端”中运行，2026-11-07）：沙箱相关 239 项全部通过，无跳过，含 5 项真实 Seatbelt 端到端；`harnessix code doctor` 显示 `[通过] action/product_native_sandbox: native_sandbox_seatbelt`。
  - 嵌套沙箱（Claude 会话内）：`doctor` 显示 `seatbelt_nested_unavailable` 及可读修复说明，验证了“沙箱不可用”时的提示。

### 16.5 第二轮决定（用户 2026-11-07）

决定原文见 ADR 0113 第 6 节，本节只记实现。

- **启动提示与诊断说明（已实现）**：
  - 零配置时，预检用一次探测同时生成命令工具和一条建议性检查 `product_native_sandbox`：可用为 `passed`，原因码为 `native_sandbox_<后端>`；不可用为 `failed`，原因码与修复标识沿用第 2.8 节映射。它不影响能否启动。显式 Action 配置或关闭零配置时不记录、不探测。
  - 预检报告仍只含稳定标识。`product_ui/preflight_text.py` 在渲染时把修复标识翻成可照做的中文说明，`harnessix code doctor` 显示为“修复动作：<标识>（<说明>）”。预检源码中出现的所有修复标识都有说明，由测试按源码扫描保证。
  - `harnessix code` 的状态栏多一段沙箱状态：“沙箱：已启用（macOS Seatbelt / bubblewrap）”，或“沙箱：不可用，命令逐条审批（运行 harnessix code doctor 查看原因）”。`harnessix exec` 在开始时把同一行写到 stderr，`--quiet` 时不写。
- **系统保留前缀（已实现）**：App Server 的 `approval/respond` 拒绝以 `harnessix:` 开头的审批人，返回 `invalid_params`，不记录决定。Harbor 适配器的 `harnessix-harbor:<模式>` 和 `exec` 的 `harnessix-exec:<模式>` 不受影响，有测试覆盖。
- **毫秒级可见窗口**：维持现状，不改内核与界面。
- **`exec --approval-mode manual`**：维持现状；需要“每条都问”的严格模式以后另议。
- **容器 Profile**：本期不自动批准，以后单独立项。
