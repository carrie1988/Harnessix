# ADR 0066：Sandbox、网络与 Secret 失败关闭边界

- 状态：已接受
- 日期：2026-09-08
- 实施：0.7.2已完成，0.7最终冷启动探测加固见`e12ae38`（2026-09-09，[CI 34268017600](https://github.com/carrie1988/Harnessix/actions/runs/34268017600)）

## 背景

Host 进程与当前用户共享权限；命令解析和审批不能阻止进程直接访问本机文件或网络。选择性域名规则还面临 DNS、IPv4/IPv6、重定向和代理绕过。Secret 若先进入模型参数或 Session，再脱敏已经太晚。

## 决策

1. 安全级别固定为 `host_guarded`、`host_sandboxed`、`container_strong`；能力报告必须区分 requested、effective 和证据。
2. `host_guarded` 只承诺 Permission、审批、环境最小化和进程归属，不宣称 OS 强隔离。
3. `host_sandboxed` 仅在 macOS Seatbelt 或 Linux Bubblewrap 后端通过预检时可用；Windows 0.7 不宣称 native host strong。
4. `container_strong` 使用固定镜像摘要、只读根文件系统、显式 bind、非 root 用户、no-new-privileges、cap-drop、PID/内存/CPU/文件限制和受控网络。
5. 网络 Profile 为 `none`、`limited`、`restricted`、`full`。`none` 必须由网络命名空间/容器网络禁用实现；`limited/restricted` 只有托管代理和隔离网络同时可用时才可生效；`full` 仍需单独策略允许。
6. `SecretProvider` 只接受版本化 `SecretRef`，在 spawn 边界临时解析并仅注入获准进程；模型、计划、Session、日志、Trace、Diff、Artifact 只保存引用和摘要。
7. 所有输出进入持久或模型边界前经过同一 `SecretRedactor`；Redactor 失败关闭输出发布，但不伪造进程未执行。
8. Container引擎`version`与`info`探测各自使用15秒有界超时，不自动重试；启动、超时或输出异常统一失败关闭为`sandbox_unavailable`。

## 取舍

- 初始 Container 端口以 Docker/兼容 CLI 为正式后端，避免自建容器运行时。
- Windows strong 优先采用 Docker Desktop 或受管 WSL2；这不替代 Windows native Workspace/Process 合同。
- 不用应用层 HTTP client allowlist 冒充任意进程网络隔离。

## 失败语义

- 后端不可用或能力不足：`sandbox_unavailable`；
- 配置请求强于有效能力：`sandbox_capability_mismatch`；
- 网络策略无法完整落实：`network_policy_unenforceable`；
- Secret 缺失/版本漂移：`secret_unavailable` / `secret_version_changed`；
- 输出脱敏失败：`secret_redaction_failed`，效果按真实执行状态记账。
