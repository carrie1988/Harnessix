# ADR 0040：Agent进程调用的单一审批权威与恢复Saga

- 日期：2026-09-05
- 基线：`81c76f6`，CI33921498948四项通过，开工fetch一致
- 状态：架构已采纳；b2b1稳定调用/Action身份契约已实现，事件迁移与运行时尚未实现

## 1. 必须解决的问题

Agent Session已有WAITING_APPROVAL和审批Item，Action Plane也有PENDING_APPROVAL和ApprovalRecord。若模型进程工具沿用普通Agent审批后再创建一个Action，或先批准Action再独立批准Agent，会产生两份都可授权执行的决定。两库提交之间任一崩溃窗口都可能导致一边批准、一边仍等待，甚至重建并重复执行命令。

SQLite Session Store与SQLite/PostgreSQL Effect Journal不是同一事务资源。本项目不引入分布式事务，也不以“通常连续成功”掩盖事实。因此必须明确唯一权威和跨库Saga，而不是复制Patch双账本方案的表面结构。Patch的受管副本可通过镜像观察归因，任意进程副作用通常不能自动核对，两者恢复能力不同。

## 2. 源码求证

- Harnessix `AgentRuntime._execute_calls/reply_approval`目前由Session审批Item消费准入；`ActionService.decide_approval`则把批准写入Effect Journal后进入READY/执行。二者都是真实授权路径，不能原样串联。
- Harnessix 0.5.4b1已证明Action Journal能持久保存命令、工具版本、审批、RUNNING租约和UNKNOWN恢复，并通过确定性幂等键返回原Action；这应成为进程执行的唯一权威，而不是被旁路。
- Codex冻结基线`a0dcfe2`的`codex-rs/core/src/session/mod.rs`集中发出并等待Exec Approval，`unified_exec/process_manager.rs`把执行请求交给命令审批策略；该分层说明审批协调与进程生命周期不同，但其内存等待模型不能直接替代本项目跨重启事实。
- OpenCode冻结基线`69c172e`的`packages/opencode/src/tool/shell.ts`先通过`ctx.ask`请求命令/外部目录权限，再由进程服务执行；本项目借鉴“先权限、后执行”的边界，不照搬Shell字符串、会话记忆放行或后台任务。
- Claude Code辅助仓库`2ca5dda`展示Bash前后台切换和持久输出路径，仅用于识别长任务需求；它不是官方安全规范，本阶段不加入后台命令。

## 3. 决策：Action审批唯一有效

进程Action的ApprovalRecord是唯一执行许可。Agent Session新增的进程审批Item只是该Action状态的受限投影，用于UI、Replay和调用归属；它自身不能直接传给Executor，也不能在缺少匹配Action事实时证明已批准。

桥接计划必须不可变绑定：Thread/Turn/Call、工作区、完整ToolCall指纹、Action ID、Action请求指纹、持久ToolDescriptor版本、宿主绑定摘要、program、argv摘要、超时和桥接策略版本。Action ID与幂等键由上述稳定身份确定生成；重试prepare只能取得同一个Action，不能创建第二条命令意图。完整argv仍只存在原ToolCall和Action请求，摘要不是审批展示替代物。

Agent的用户答复流程调整为：

1. 复核当前Thread/Turn/Call和计划，调用Action审批接口写入唯一决定；
2. 读取返回Action，核对ApprovalRecord的actor/outcome/reason/request_fingerprint；
3. 才把决定及Action状态投影进Session审批Item；
4. 后续执行/等待只按Action ID读取既有Action，不再调用`decide_approval`。

若Action已存在相同决定，步骤1是读取确认；不同决定必须冲突。Agent Session中的“批准”字段必须由桥接层根据Action事实构造，不能接受模型、客户端或普通`ApprovalRecord`直接注入。

## 4. 跨库崩溃矩阵

| 窗口 | Action事实 | Session事实 | 恢复动作 |
|---|---|---|---|
| 创建Action前退出 | 无 | 普通未结算ToolCall | 用稳定身份重新prepare一次 |
| Action已PENDING、审批Item提交前退出 | PENDING | 无投影 | 重取同一Action，再提交投影 |
| 审批Item已等待、用户答复前退出 | PENDING | WAITING | 重开只读取，继续等待 |
| Action决定提交前退出 | PENDING | WAITING | 不猜决定，继续等待/允许用户重答 |
| Action决定已提交、Session投影前退出 | READY/RUNNING/终态 | 仍WAITING | 读取并核对Action决定，只补投影，不再批准 |
| Action运行时Agent退出 | RUNNING | 已投影决定 | 重开只观察；不创建Action、不重放 |
| Action终态、Session结果前退出 | 终态 | 无ToolResult | 从原Action生成一次结果/Artifact并提交 |
| Action租约过期 | UNKNOWN | 已投影决定 | Agent发布unknown并停止循环；不得回READY |

恢复不得根据Session缺结果推断Action未执行，也不得根据PID消失推断没有外部副作用。旧工具版本、跨Thread/Turn/Call/工作区、不同Action指纹或不同批准决定一律拒绝补投影。

## 5. 等待状态和取消

当前Turn只有WAITING_APPROVAL，没有“Action已批准但仍由Worker运行”的持久等待状态。b2实现不能用长轮询阻塞`reply_approval`，也不能把RUNNING当UNKNOWN。下一契约片应新增明确的WAITING_ACTION状态/事件，保持原Turn墙钟预算不刷新；前台取消只停止等待并请求当前本机生命周期清理，不能撤销已提交Action决定或宣称远端Worker/逃逸后代已终止。

恢复流程仅观察Action快照：PENDING继续审批，READY/LEASED/RUNNING继续等待，SUCCEEDED/FAILED生成结果，UNKNOWN/MANUAL_INTERVENTION生成unknown并终止Agent循环。自动观察必须有界；后台通知、无限轮询和会话级永久放行均非本片范围。

## 6. Process Artifact

Action Result是效果事实，Session Artifact是模型展示材料，两者不可互相冒充。桥接先读取终态Action和完整ProcessResult，再生成有界公开预览；完整二进制双流使用独立`process_output`用途发布。Artifact正文、manifest、引用与Agent ToolResult应在Session/Artifact库同事务，发布失败只能省略展示引用或在恢复时重建，不能改写Action终态。

Artifact至少绑定Action ID/指纹、Call ID、流名、observed字节数/摘要、EOF和捕获正文摘要。stdout/stderr用途或分片必须唯一，分页/总配额/TTL/活跃Thread保护沿用现有Artifact规则。argv可能包含源码或敏感值，不进入输出Artifact；当前SecretRef未实现，命令仍禁止承载凭据。

## 7. 版本与实施切片

b2实现会新增Agent审批/等待/结果投影，需升级Agent Event/Thread reader和Session migration；具体版本号只在兼容夹具和迁移方案完成时确定，不能先手改标签。Action Contract/Journal、Process v1 Schema和0.5.4b1工具保持不变。

实施拆分：

1. b2a（本ADR）：冻结唯一权威、计划身份、Saga矩阵、等待状态和Artifact边界；
2. b2b：实现桥接契约、Agent事件/迁移及旧reader真实兼容；其中b2b1已交付确定性Action身份、请求构造/快照核对与冻结计划Schema，b2b2继续事件和迁移；
3. b2c：实现Agent Runtime执行/恢复、Process Artifact及双SDK离线闭环；
4. 0.5.4c：在同一准入上增加固定Git/run_tests，最后才评估受控Shell。

本ADR不宣称已实现Agent进程工具、跨库原子提交、宿主死亡自动清理或OS Sandbox。
