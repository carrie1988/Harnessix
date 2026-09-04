# ADR 0032：整组事务预留与持久审批

- 日期：2026-09-04
- 基线：`09cb6d6`，CI `33842477262` 四项通过
- 状态：c2a 已交付；c2b 后续实现见 ADR 0033，本 ADR 保留审批层历史边界

## 1. 分片与复用边界

按 ADR 0031 继续推进 c2，但将其分成可独立验收的两片：c2a 完成真实账本迁移、整组预留和持久审批；c2b 再实现顺序消费、部分/未知效果及只核对恢复。先关闭旧单文件接口拆分消费整组成员的路径，再引入组执行器，避免处于中间版本时批准意外获得写权限。

复用既有 PreparedPatchBatch、单文件镜像/事件验证器、受管副本互斥锁和 SQLite 事务。宿主入口为 `ManagedPatchBatches(copy)`，借用 copy 的生命周期与锁，不创建第二连接/锁/线程。c2a 没有 execute 接口，不生成未来执行状态的空表；c2b 现使用独立执行记录，保持本片不可变计划与审批契约含义不变。

## 2. 公共契约

`ManagedPatchBatchPlan/v1` 绑定 batch_id、workspace_id、宿主稳定 request_id、完整 PatchBatchManifest、有序成员身份以及 approval_fingerprint。每个成员绑定独立 plan_id、确定性 request_id 和既有单文件 approval_fingerprint；位置参与 request_id 摘要。组指纹覆盖上述全部内容，不覆盖后续决定。组 ID 和成员 ID 不复用已有单文件计划。

`ManagedPatchBatchApproval/v1` 包含完整计划和可空 ApprovalDecision。空决定表示等待；批准/拒绝是不可变的宿主决定，不表示任何成员已执行。宿主 request_id 是当前绑定上限，并非已验证的 Thread/Turn/Call；c3 才增加真实 Kernel 调用绑定。不能将计划 Diff（尤其截断展示）作为批准对象。

save 先验证完整私有载荷，再按稳定请求查已有组。完全相同重试返回原组；同请求内容或顺序变化报 request_conflict。新组必须复核全部当前前镜像和副本路径准入。lookup/get 只读取，不隐式准备或创建。reply 按组 ID 和完整指纹保存决定，同决定重试幂等，不同决定报 approval_conflict。reply 不重新计算计划、不执行写入；审批后来源变化由后续执行前复核拒绝。

## 3. 副本账本 v2

保持原 metadata、baseline、plans 镜像和 events 原字节。新增 batches（完整计划及校验和）、batch_approvals（唯一最终决定及绑定校验和），plans 新增可空 owner_batch_id 外键；NULL 为既有独立单文件计划。组顺序以完整计划中的成员列表为准，加载时交叉检查所有归属行与既有单文件记录，不信任仅传入的摘要。

c2a 时新副本初始化 v2（当前 c2b 会继续至 v3）；旧 v1 在已有独占 owner.lock、身份/metadata/baseline 校验之后，在同一事务中验证全部旧计划、执行 DDL 并推进 user_version。失败回滚，不能先写版本再补表。不改写旧事件、不替换数据库 inode。未来版本与错误 application_id 拒绝。原 v1 wheel 会按其既有版本检查拒绝 v2；不得手动降低 user_version。升级前应关闭使用者并整体备份私有副本目录；有新状态后不支持无损降级。

组记录、所有成员 plans/初始 pending events 在同一 BEGIN IMMEDIATE 事务内创建。共同占用已有 64 计划、32 MiB 前后镜像限额；单文件保存也在事务内检查同一配额。另限制组计划单份 UTF-8 JSON 64 KiB，组元数据逻辑预留总量 1 MiB，每组按计划实际字节加 16 KiB 决定预留计费；reply 不会因其他组占满配额而失去决定空间。该限制不代表 SQLite 文件、日志或总进程内存上限。

加载校验组/决定校验和、完整契约、工作区、稳定请求、成员顺序、归属、单文件事件和完整镜像。SHA 校验和用于一致性而非抵御同 UID 恶意重写整本账本。旧 save 幂等命中、reply、execute 拒绝组成员，并交叉检查完整组计划，不能将被清空的 owner 列当成独立单文件授权。组决定不镜像为各成员批准，成员继续保持 pending。get/lookup/verify 仍可只读检查单成员。

## 4. 后续 c2b 的执行边界（已由 ADR 0033 实现）

组意图持久化后即消费旧批准；执行前整组复核，每成员写前复核。复用单文件执行与归因，不新增替换引擎。严格顺序，首个失败、未知或取消停止后续调度；成功前缀保留，未开始者不记成执行失败。组终止原因与全未应用/全已应用/已知部分/含未知效果分离。恢复只核对已有成员，不创建/批准/重放；observed_before 不允许续用批准。新增独立执行记录与迁移，不扩大本片审批契约含义。

## 5. c2a 验收

- 严格绑定、顺序/内容冲突、路径准入、取消/超时、来源漂移；幂等查找及批准/拒绝；任何组成员均不能通过旧单文件入口写入。
- 与既有计划共享配额、元数据预留、事务内成员任意位置失败；真实退出覆盖迁移、整组预留和决定提交前后，重开仅查询，不改变源目录/副本文件 inode、mtime、ctime。
- 从实际 `09cb6d6` wheel 创建 pending/approved/applied 旧计划，新 wheel 升级保留旧事件原字节，旧 wheel 拒绝新账本；不以手动标记版本替代升级证据。
- 旧 Schema 不变、新 Schema 冻结，全量回归、独立基础 wheel 和跨平台 CI；不新增模型请求、SDK 依赖或中间件。
