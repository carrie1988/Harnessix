# ADR 0035：Kernel 整组审批、效果与跨账本恢复

- 日期：2026-09-04
- 基线：`5a09dd0`，CI `33869981047` 四项通过
- 状态：c3b 已实现并完成本地/独立 wheel 验收，跨平台以本片 CI 为准；Diff Artifact 仍属于 c3c

## 1. 已核对的实现与决策

核对 `agent/models.py`、`approvals.py`、`reducer.py`、`patching.py`、`runtime.py`、`session/sqlite.py`、`models/_history.py` 和 c3a 桥接。复用同一调用、审批事务、状态投影器及模型循环，不构建第二套 Runtime。新增宿主显式 `patch_batches` 专用端口；默认不广告批量写，旧 `patches` 单文件接口不变，任意通用写工具仍拒绝。

## 2. Agent v7 与 Session migration8

新增 `patch_batch_approval_request` Item，计划直接复用完整 `ManagedPatchBatchCallPlan/v1`。独立策略 `kernel-managed-patch-batch/v1`，请求与决定必须绑定完整调用批准摘要；旧只读/单文件审批不能批准整组。批准时只保存 Session 决定，副本成员仍 pending。

ToolResult 新增可空私有 `patch_batch`：副本/组/稳定请求/外层审批身份、execution/recovery 来源，以及可空的既有 `BatchExecutionResult/v1`。实际运行存在时必须已终止，并与组身份及有序成员完全匹配；没有运行对应尚未开始，不伪造文件事件。该证据上限8 KiB，不占公开输出预算，不进入模型 wire。旧 `patch` 字段与全部旧 Schema 不改；两个证据不能共存，新字段为 None 时序列化省略，旧事件导出保持原结构。新组审批/证据不能贴 v1–v6 标签。

Event/Thread 新 Schema v7，投影写入7、兼容读取1–7。migration8 仅最低 reader 标记，不创建新表，不重写旧事件/投影；旧 reader 明确拒绝新库。副本账本保持v3。以真实 `6a7cc65` wheel 创建旧只读/单文件审批，验证升级后原字节不变与旧 reader 拒绝，不手改版本伪造旧包。

## 3. 准入与时限

调用落库→桥接完整组计划→Session WAITING 审批→宿主决定→持久离开 WAITING→后端镜像决定/整组一次性执行→Session 结果。两个库非原子。Kernel 在执行前再次校验当前活跃 Thread/Turn、首个未结算调用、工具契约、完整计划/批准、原截止时间；检查上下文不等于执行许可。

准备和执行在原 `_continue` 超时范围内；组审批 review 也按持久 Turn 剩余时间限制，并在提交决定前复核。取消/超时/关闭必须排空工作线程；暂停/离线/重开不能刷新 Turn 预算。桥接的5秒单操作预算只可缩短，不可替代原 Turn 生命周期。

## 4. 效果、终止与恢复

- 正常全部应用继续模型；拒绝产生未开始的已知失败结果，可让模型说明拒绝。
- 已启动组的 failed/timeout/cancelled 停止当前 Turn，不在同一调用内补跑后缀；效果与原因分开，最后写后取消可以全部已应用而 Turn 取消。
- 任一未知效果终止为 interrupted，禁止自动继续模型。公开输出超限也通过专用恢复结算真实私有效果，不能抹去已写前缀。
- 重开保留未过期 WAITING；其他未终态只核对原组，不能 prepare/save/reply/execute。原完整计划、宿主决定、后端批准或存储事实缺失/错绑，保持 unknown。
- 因 c3a 保守证明策略，取消 WAITING 或 Session 决定已存在但后端未镜像时，可结束为 interrupted/unknown；这不表示已写入，而表示没有完整匹配的后端批准证据。不得为了得到 cancelled 而补批或伪造未应用。
- 已持久 ToolResult 不再次核对；恢复产生的结果不能把 Turn 改称正常完成。结果与批准/运行/成员顺序错绑在在线提交与 Replay 统一拒绝。

## 5. 验收门禁

双实际 SDK 的离线读取→批量提案→持久审批重开→真实多文件修改→读回；默认关闭、严格输入、作用域/定义/计划/批准错绑、拒绝、源漂移、已知部分/未知、输出预算、原时限、Token/Task/重复关闭。Session 与副本各提交窗口、所有成员意图/替换/结果以及结算时再次退出须用真实子进程验证；恢复禁止任何写入口且源目录/目标 inode/mtime/ctime 不变。真实旧 wheel 升级、历史 Schema 与混合 Replay、基础发行包、全量/异步测试及四项 CI。所有模型测试离线，无新模型授权预算，无远程部署。

## 6. 实施结果与后续

实现专用端口、完整组审批、Agent v7 私有效果和 migration8；没有新增副本表或重写后端。额外修复旧/新写端口缺失时 WAITING 结算循环，补充旧单文件回归。真实旧 wheel 升级与旧 reader 拒绝、迁移事务退出、Session 存储回滚/丢失确认、双 SDK 离线以及每成员崩溃/取消均有证据，见 [测试第27节](../testing-and-evals.md#27-053c3b-kernel-整组闭环验收2026-09-04)。c3b 范围完成不代表 c3/0.5 完成，下一片 c3c 单独实现 Diff Artifact，不扩大当前写路径。
