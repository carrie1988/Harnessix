---
doc_type: adr
status: current
version: 1
code_revision: 5f50d4b705712fc4f035a5b4f799f4da0f5cee85
owners:
  - core
modules:
  - processes
  - workspace
related_adrs: []
related_tests:
  - tests/processes
supersedes: []
---

# ADR 0067：跨平台进程所有权与终端生命周期

- 状态：已接受
- 日期：2026-09-08

## 背景

0.5 Process 只支持 POSIX、单前台 argv、关闭 stdin。生产 Coding Agent 需要受控 Shell、PTY、stdin、后台任务、断线恢复和宿主死亡清理。数字 PID 会复用，不能作为跨重启执行许可。

## 决策

1. 进程请求拆分为不可变 `ProcessSpec` 和持久 `ProcessLease`；Lease 拥有稳定 process id、计划 fingerprint、状态、owner 证据、输出 Artifact 和最后事件序号。
2. `argv` 为默认模式；Shell 必须显式选择 `posix_sh`、`cmd` 或 `powershell`，原文作为不可变参数参与审批，绝不由 Runtime 拼接。
3. stdin 默认关闭；pipe 和 PTY 是显式能力。PTY 输入、resize、close 和输出都关联同一 Lease，并有字节/频率上限。
4. POSIX 使用新 Session/Process Group；Windows 使用不可 breakaway Job Object，并以挂起创建→绑定→恢复消除子进程逃逸窗口。
5. foreground 在 Turn 终态前结算；background 必须持久化 Lease，宿主重启只核对 owner/输出/终态，不凭 PID 自动重发命令。
6. 超时、取消、输出上限、关闭和宿主死亡都先停止完整 owner，再等待回收并记录 `exited/terminated/killed/cleanup_failed/unknown`。
7. 输出内存有界，超额内容流式进入 Artifact；最终结果包含观察字节数、摘要、截断和 EOF，不把截断误报完整。
8. Rust Sidecar 暂不引入；Python 端口若无法在 Windows 消除创建竞态或 PTY 生命周期经基准不达标，再由独立 ADR 决定。
9. Container 执行使用 `ContainerExecutionSpec` 将内层命令、Process 生命周期和 owner 能力绑定，再以 `ProcessLaunchBinding` 固定批准计划到实际容器客户端进程的转换；容器名称和标签只由 process id 与执行摘要派生。
10. 容器客户端进程仍由同一 POSIX Session/Windows Job owner 监督，但其退出不等于容器工作负载已经结束；正常、取消、超时、启动失败和恢复都必须按名称及双标签清理容器，并在再次查询为空后才能报告产品级终态。
11. 选择性网络在 spawn 前即时复核受管 internal network、策略/网关标签和唯一网关容器；后端不能提供等价证明时失败关闭，不把规划阶段的陈旧网络证明继续用于执行。

## 失败语义

- owner 建立前失败：未启动；
- 启动后、Lease 提交前失败：`UNKNOWN`，只能按 owner token/输出标记/进程句柄核对；
- 终止失败：Runtime 熔断该 executor，不接收新进程；
- 宿主重启后 owner 不可证明：`orphaned_unknown`，不自动执行或发送输入。
- 容器客户端已退出但实例存在、身份冲突或无法查询/清理：`process_cleanup_failed`，不得把内部 Lease 的退出状态作为容器执行成功；
- 选择性网络无法即时复核：`network_policy_unenforceable`，不得启动容器。
