# ADR 0065：平台能力端口与不可变执行计划

- 状态：已接受
- 日期：2026-09-08

## 背景

0.6 的 Workspace 和 Process 实现直接依赖 POSIX；现有审批摘要绑定 Tool Call 和 Workspace 标签，但没有把 cwd、环境摘要、Workspace 内容快照、Sandbox/Network Profile 一次性冻结。直接在现有类中增加 `if os.name` 会混淆领域路径、宿主路径和 OS 安全原语。

## 决策

1. 领域层新增平台中立的 `WorkspacePath`、`WorkspaceSnapshot`、`ExecutionIntent` 与 `ExecutionPlan`。
2. `WorkspacePath` 只能是相对 Workspace 的 `/` 分隔逻辑路径；宿主盘符、UNC 和设备路径只存在于受信平台端口。
3. POSIX 与 Windows 分别实现路径打开、对象身份和进程所有权，不共享虚假的最小公分母。
4. `ExecutionPlan` 使用规范 canonical JSON 计算 fingerprint，至少绑定：Tool 名称/版本/来源、规范参数、cwd、非 Secret 环境摘要、Workspace Snapshot、策略版本、Sandbox、网络、Secret 引用版本、效果、风险和幂等键。
5. 审批 UI、执行器和审计只消费同一个计划；任何字段变化必须重新规划和审批。
6. 不可证明的外部 Workspace 变更使计划过期；禁止执行阶段静默刷新 snapshot。

## 取舍

- 不对整个大型仓库每次做无界内容哈希；Snapshot 由根身份、Git/目录观察和计划涉及资源构成，并明确覆盖范围。
- Host 通用命令无法穷举未来写集合，因此高风险命令优先在隔离副本执行；Host 批准不被描述为全仓事务锁。
- 现有 `request_fingerprint` 保留兼容，由新计划桥接，避免一次性重写历史 Session。

## 失败语义

- 非法领域路径：`workspace_path_denied`；
- 平台能力缺失：`workspace_platform_unsupported`；
- Reparse/symlink/对象身份变化：`workspace_changed`；
- 计划字段或 Snapshot 漂移：`execution_plan_stale`；
- 指纹不一致：`execution_plan_mismatch`。

## 验证

- POSIX 和 Windows 路径属性测试；
- Windows 盘符、UNC、设备路径、ADS、保留名、尾随点/空格和 Reparse Point；
- 参数、cwd、环境、Workspace、策略、Sandbox、网络和 Secret 版本逐字段变异测试。

