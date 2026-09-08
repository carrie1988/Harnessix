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

## 失败语义

- owner 建立前失败：未启动；
- 启动后、Lease 提交前失败：`UNKNOWN`，只能按 owner token/输出标记/进程句柄核对；
- 终止失败：Runtime 熔断该 executor，不接收新进程；
- 宿主重启后 owner 不可证明：`orphaned_unknown`，不自动执行或发送输入。

