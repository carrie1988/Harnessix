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
| 2 | bubblewrap 参数翻译与执行，247 宿主机验证 | 未开始 |
| 3 | Seatbelt 参数翻译与执行（含已配置代理端口拦截） | 未开始 |
| 4 | 审批策略 v2、沙箱外命令、零配置装配、诊断 | 未开始 |
| 5 | 三环境验收；回写 ADR 0066 第 2 版与模块设计 | 未开始 |

## 4. 总体架构

| 层 | 职责 | 源码 |
|---|---|---|
| 探测 | 判断本机后端是否可用，给出身份、原因码与保证项 | `sandbox/native_probe.py` |
| 策略 | 从本机事实推导一个工作区的完整边界与命令环境，平台无关 | `sandbox/native_policy.py`、`sandbox/native_contracts.py` |
| 翻译（第 2、3 步） | 把策略翻译成 `bwrap` 参数或 Seatbelt 策略加 `-D` 参数 | 待新增 |
| 装配与执行（第 4 步） | 零配置生成 Profile，绑定执行计划，经现有宿主执行器运行 | `product_config/native_process.py`、`host_process_action.py` |
| 审批（第 4 步） | 沙箱内命令由策略直接放行，沙箱外命令逐条批准 | `trusted_actions/policy.py` |

策略层只做确定性推导，不读写文件系统；采集本机事实与执行命令都放在边缘，便于测试和审计。

## 5. 流程与数据流

1. 启动时探测后端，得到 `HostSandboxProbeV2`。
2. 采集本机事实：HOME、工作区、Git 目录、Harnessix 状态与配置路径、代理端口、私有目录。
3. 由本机事实推导 `NativeSandboxPolicy`，它的摘要计入执行计划的 `SandboxBindingV2.profile_digest`。
4. 每条命令按计划翻译成后端参数（第 2、3 步），由 POSIX Process Owner 监管执行。

## 6. 接口设计

| 函数 | 输入 → 输出 |
|---|---|
| `probe_native_sandbox(platform=…, runner=…, nested=…)` | 平台与可替换的执行器 → `HostSandboxProbeV2` |
| `build_native_sandbox_policy(facts)` | `NativeSandboxFacts` → `NativeSandboxPolicy`；冲突时报 `KernelError` |
| `workspace_git_directories(workspace)` | 工作区 → Git 目录元组（含 worktree 的 gitdir 与 common dir） |
| `loopback_proxy_ports(urls)`、`environment_proxy_urls`、`macos_system_proxy_urls` | 代理设置 → 本机回环代理端口 |
| `sandbox_environment(...)`、`host_environment(...)` | 启动环境、私有目录、预设 → 沙箱内或沙箱外命令的环境 |
| `sandbox_reserved_environment_problem(name)` | 变量名 → 是否由 Harnessix 管理（Profile 预设不得覆盖） |

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
- 执行期的超时、取消、重启对账沿用 POSIX Process Owner（第 2、3 步接入）。

## 9. 持久化、事务与数据流程

- 第 1 步不新增持久化。策略摘要进入执行计划后，随现有计划与路由账本持久化。
- 私有缓存目录（ADR 0111 第 2.3 节）在第 2、3 步创建，位于用户缓存目录，不在 Harnessix 状态库内。

## 10. 安全、权限与信任边界

- 拒读清单涵盖：凭据、命令历史、浏览器与钥匙串、Harnessix 状态与配置、工作区 `.harnessix`。
- 可写目录与拒读清单冲突时失败关闭。
- 命令环境只继承白名单内的变量，模型凭据与 `HARNESSIX_*` 永不继承；代理变量只给沙箱外命令。
- 可执行文件只查固定路径，不受 PATH 劫持。

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
| `native_backend_unsupported` | 后端尚未接入执行器 |

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
- 247 实测：以 `setpriv` 降权为 `nobody` 后，bubblewrap 加 `--unshare-all` 运行成功。

## 13. 源码映射

| 关注点 | 源码 |
|---|---|
| 探测与策略合同 | [`native_contracts.py`](../../src/harnessix/sandbox/native_contracts.py) |
| 策略推导、代理端口、环境白名单 | [`native_policy.py`](../../src/harnessix/sandbox/native_policy.py) |
| 后端探测 | [`native_probe.py`](../../src/harnessix/sandbox/native_probe.py) |
| Profile 合同 | [`action_contracts.py`](../../src/harnessix/product_config/action_contracts.py) |
| 外部边界证明的后端守卫 | [`native_process.py`](../../src/harnessix/product_config/native_process.py) |
| `SandboxBinding` 规则 | [`execution/contracts.py`](../../src/harnessix/execution/contracts.py) |

## 14. 部署、兼容与回退

- **兼容**：
  - 新字段取默认值时不序列化，已有 Action 配置与外部边界 Profile 的摘要不变。
  - 新增 `spec/host-sandbox-probe-v2.schema.json` 与 `spec/native-sandbox-policy-v1.schema.json`；Action 配置相关 Schema 由生成脚本更新。
- **回退**：第 1 步不改变任何运行时行为。新后端在配置中出现时一律被省略。回退只需还原本步提交。

## 15. 风险与取舍

- 拒读清单需要持续维护；清单之外的文件可被读取（ADR 0111 第 4 节）。
- bubblewrap 预检的报错分类依赖报错文本，第 2 步要在命名空间被拒的容器内实测补全。
- 参数数量受 `ProcessSpec` 128 项上限约束，第 2 步实测常见机器上的余量。
