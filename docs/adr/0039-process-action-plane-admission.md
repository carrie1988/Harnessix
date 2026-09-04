# ADR 0039：复用Action Plane的持久命令准入

- 日期：2026-09-05
- 基线：`adfe267`，CI33899008420四项通过，开工fetch一致
- 状态：已采纳；0.5.4b1实现，不代表0.5.4b全部完成

## 1. 问题与源码求证

0.5.4a只有受信宿主进程生命周期。如果直接把它注册给模型，命令不会进入持久意图、策略、人工审批、租约与未知效果恢复；如果在`processes`下再建一套命令账本，则与项目已有Action Plane重复，还会产生两个审批真相源。

本片复核现有 `ActionService`、SQLite/PostgreSQL `EffectJournal`、`ActionWorker` 和 `DefaultPolicyEngine`：非幂等写可以在执行前持久化、要求审批、用租约标记RUNNING，进程退出导致租约过期时由Journal转成UNKNOWN，且不会自动回到READY。现有Executor端口足以承载宿主进程，不需要新增数据库迁移或平行状态机。

对照源码仅提炼边界，不复制实现：Codex冻结基线`a0dcfe2`把命令审批、执行环境与Sandbox分层；OpenCode冻结基线`69c172e`的 `packages/opencode/src/tool/shell.ts`在执行前分析命令与目录权限，`packages/core/src/process.ts`负责进程I/O；Claude Code辅助仓库`2ca5dda`仅用于观察Bash任务形态。本片没有照搬Shell字符串、后台任务或会话级自动放行，也不把非官方仓库当安全规范。

## 2. 决策

新增宿主显式工厂 `process_action_tool(factory)`，返回普通 `ToolDefinition`，但不加入默认Bootstrap、更不加入Agent模型工具清单。固定属性为：

- `NON_IDEMPOTENT_WRITE`、HIGH风险、必须幂等键、必须审批；
- 输入仍是命名程序、argv和可缩短超时，没有shell字符串/cwd/env字段；
- 工厂每次提供独立`HostProcessRuntime`，避免在并发Action间共享活动进程状态；
- 工具版本包含cwd、程序身份、环境和资源策略的SHA-256绑定摘要；摘要不暴露环境值；
- 执行前同时比较持久ToolDescriptor、当前Executor版本和新Runtime绑定。审批后配置漂移只失败，不运行新配置；
- `secret_refs`在没有受信解析器前明确失败，绝不把引用名称当作凭据值或环境注入。

Action Plane是唯一持久准入事实：Action请求及不可变工具描述先保存，Policy要求审批，批准后才进入READY/LEASED/RUNNING。幂等键只去重完全相同的Action请求，不承诺命令本身可重入；工具版本变化不能消费旧批准。

## 3. 结果和未知效果

Action `SUCCEEDED`表示一次进程调用已经得到确定生命周期结果，不表示退出码为零、测试通过或命令没有修改文件。真实returncode、stop_reason、termination和双流证据完整保存在`ProcessResult`，Effect Receipt只记录结果摘要和原幂等键，不把PID当外部资源身份。

组清理失败或任一管道非自然EOF时，Action为UNKNOWN，同时保留有界ProcessResult和Receipt。超时/非零退出若已取得完整终止证据，仍是确定的进程调用结果；调用方必须读取ProcessResult判断测试语义。FAILED仅用于已证明未启动的输入、凭据或绑定错误。所有失败均禁止自动重放；用户若主动创建新Action属于新意图。

调用Task取消时，0.5.4a先终止并回收直接子进程，Action若来不及写终态会保留RUNNING，租约过期后转UNKNOWN。Worker或宿主`os._exit`时，Journal同样只恢复UNKNOWN。对账进入MANUAL_INTERVENTION，不调用执行器、不按历史PID/进程组号发送信号、不重新运行命令。测试明确证明硬退出后测试子进程仍可能存活，并由测试父进程清理；这是事实恢复，不是进程containment。

## 4. 边界与后续

本片没有修改Agent v8、Session migration9、Provider v3、副本v3、既有Schema或依赖；Process Action使用0.1的SQLite/PostgreSQL Journal格式，无新迁移。没有默认开放命令，没有OS Sandbox、脱组后代治理、宿主死亡自动清理、Secret解析、Process Artifact、Git/run_tests或模型桥接。

0.5.4b2需决定Agent Session与Action Plane之间的稳定调用/审批/结果绑定，避免Agent审批与Action审批成为双真相；同时设计长输出Artifact及硬退出后的运维处置。0.5.4c再提供固定Git和测试工具，受控Shell最后开放。更强进程树/文件/网络隔离仍属于0.7，不能用Journal的UNKNOWN状态冒充隔离成功。
