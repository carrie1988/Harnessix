---
doc_type: adr
status: current
version: 2
code_revision: d5f34d25d125f952624f8f7b1f76de6d6468bb09
owners:
  - core
modules:
  - sandbox
  - secrets
related_adrs:
  - docs/adr/0111-native-process-sandbox-backends.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
related_tests:
  - tests/sandbox
  - tests/secrets
supersedes: []
---

# ADR 0066：Sandbox、网络与 Secret 失败关闭边界

- 状态：已接受
- 日期：2026-09-08；第2版 2026-11-08（`host_sandboxed`落地为Seatbelt与bubblewrap执行后端，见ADR 0111）
- 实施：0.7.2已完成，0.7最终冷启动探测加固见`e12ae38`（2026-09-09，[CI 34268017600](https://github.com/carrie1988/Harnessix/actions/runs/34268017600)）

## 背景

Host 进程与当前用户共享权限；命令解析和审批不能阻止进程直接访问本机文件或网络。选择性域名规则还面临 DNS、IPv4/IPv6、重定向和代理绕过。Secret 若先进入模型参数或 Session，再脱敏已经太晚。

## 决策

1. 安全级别固定为 `host_guarded`、`host_sandboxed`、`container_strong`；能力报告必须区分 requested、effective 和证据。
2. `host_guarded` 只承诺 Permission、审批、环境最小化和进程归属，不宣称 OS 强隔离。
3. `host_sandboxed` 仅在以下后端通过预检时可用，每个后端实际生效的保证项写入能力报告；Windows 不宣称原生强隔离（第2版）：
   - macOS Seatbelt；
   - 能创建用户命名空间的 Linux 宿主机上的 bubblewrap。
4. `container_strong` 使用固定镜像摘要、只读根文件系统、显式 bind、非 root 用户、no-new-privileges、cap-drop、PID/内存/CPU/文件限制和受控网络。
5. 网络 Profile 为 `none`、`limited`、`restricted`、`full`。`none` 必须由网络命名空间/容器网络禁用实现；`limited/restricted` 只有托管代理和隔离网络同时可用时才可生效；`full` 仍需单独策略允许。第2版补充：`none` 只能由网络命名空间或禁用的容器网络实现；放行宿主回环的 Seatbelt 沙箱不得记为 `none`，应记为 `full`，并在能力报告中列出“非回环出站被拦截”。
6. `SecretProvider` 只接受版本化 `SecretRef`，在 spawn 边界临时解析并仅注入获准进程；模型、计划、Session、日志、Trace、Diff、Artifact 只保存引用和摘要。
7. 所有输出进入持久或模型边界前经过同一 `SecretRedactor`；Redactor 失败关闭输出发布，但不伪造进程未执行。
8. Container引擎`version`与`info`探测各自使用15秒有界超时，不自动重试；启动、超时或输出异常统一失败关闭为`sandbox_unavailable`。

## 取舍

- 初始 Container 端口以 Docker/兼容 CLI 为正式后端，避免自建容器运行时。
- Windows strong 优先采用 Docker Desktop 或受管 WSL2；这不替代 Windows native Workspace/Process 合同。
- 不用应用层 HTTP client allowlist 冒充任意进程网络隔离。

## 失败语义

- 后端不可用或能力不足：`sandbox_unavailable`；原生沙箱探测与装配的原因码（如`seatbelt_nested_unavailable`、`bubblewrap_namespaces_denied`，完整列表见ADR 0111第5节）均归入此类，不静默降级为无隔离执行；
- 配置请求强于有效能力：`sandbox_capability_mismatch`；
- 网络策略无法完整落实：`network_policy_unenforceable`；
- Secret 缺失/版本漂移：`secret_unavailable` / `secret_version_changed`；
- 输出脱敏失败：`secret_redaction_failed`，效果按真实执行状态记账。

## 第2版：原生进程沙箱后端（2026-11-08）

- 背景：第1版定义了`host_sandboxed`，但代码只有预检，没有执行后端。ADR 0111 为原生进程 Profile 实现了 macOS Seatbelt 与 Linux 宿主机 bubblewrap 两个后端，ADR 0113 让沙箱内命令由宿主以系统身份自动批准、沙箱外命令逐条人工批准。
- 修订：第3条改为按后端列出可用条件与能力报告；第5条补充`none`与回环放行的记法；失败语义补充原生沙箱原因码的归类。修订内容来自 ADR 0111 第2.9节，随实现合入主线时写回。
- 验收：macOS 用户终端沙箱相关 239 项、247 以`nobody`身份 282 项全部通过，记录见`docs/changes/native-process-sandbox.md`第12节。
