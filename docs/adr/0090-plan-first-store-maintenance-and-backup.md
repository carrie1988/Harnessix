---
doc_type: adr
status: current
version: 1
code_revision: cb3f3ea834624d5a8f84396952eba212650065d1
owners:
  - core
modules:
  - session
  - protocol
  - artifacts
related_adrs:
  - docs/adr/0010-session-store-and-recovery.md
  - docs/adr/0072-durable-interaction-and-pull-live-stream.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0089-bounded-local-transport-lifecycle.md
related_tests:
  - tests/agent/test_store_maintenance.py
  - tests/agent/test_store.py
  - tests/agent/test_process_session_upgrade.py
  - tests/artifacts/test_batch_diff_upgrade.py
  - tests/artifacts/test_process_output_upgrade.py
supersedes: []
---

# ADR-0090：Session共库采用Plan-first维护、保守禁删与强制备份恢复

## 状态

接受并由0.9.3b实现，代码Revision为`cb3f3ea834624d5a8f84396952eba212650065d1`。本决策的本地全仓门禁为
`3589 passed, 32 skipped`；三平台、Container和文档CI仍是0.9.3b路线图关闭条件，CI完成前不得把本地结果描述为正式发布验收。

本ADR只定义单一Coding Agent内部SQLite共库的离线维护能力，不恢复独立Action Plane、HTTP API、Worker、后台GC服务或
远程数据库依赖。

## 背景

Harnessix Code把Session Event/Thread投影、Agent Protocol幂等请求和Artifact正文保存在同一个受应用身份保护的SQLite文件中。
这种部署拓扑减少了本地产品的运维单元，但也形成三类长期增长事实：

1. `agent_events`和`agent_threads`随Thread数量与历史长度增长；
2. `protocol_requests`为Command响应丢失恢复保留`accepted/completed/failed`记录；
3. `agent_artifacts`保存完整Tool、Diff、Review和Action Output正文，TTL回收只清空正文并保留Tombstone。

0.9.3a只限制stdio/SDK内存与关闭生命周期，不能回答“哪些持久事实可以删除”“崩溃后从哪里继续”“误删如何回滚”。
直接按TTL执行`DELETE`会同时破坏Session引用、协议幂等、UNKNOWN效果恢复和审计证据；只观察SQLite文件大小也无法区分
WAL、空闲页、正文和逻辑记录。持久维护必须成为有正式合同、失败语义和恢复证据的产品内部能力。

源码研究和现状证据见[可靠性、背压与长期运行源码研究](../research/reliability-and-performance.md)，完整实现流程见
[0.9.3b持久容量与保留详细设计](../changes/m09-3b-persistent-capacity-and-retention.md)。

## 决策驱动因素

1. 活跃Thread、未决Command、UNKNOWN/RECONCILING效果和Fork来源不能被清理；
2. 维护选择必须可审阅、可重算、可检测篡改，不能扫描到一行就立即删除；
3. 崩溃恢复必须知道精确的下一候选，不能从当前数据库重新猜测已执行范围；
4. 业务变更与维护进度必须在同一个SQLite事务提交；
5. 任何破坏性执行前必须生成完整、可验证且属于该计划的备份；
6. Protocol Store只保存参数摘要，无法从`accepted`记录恢复目标Thread/Artifact身份；
7. 公开容量与执行结果不得泄漏Thread ID、路径、Prompt、源码、Tool正文或Secret；
8. Maintenance属于唯一Coding Agent Runtime的内部端口，不得成为第二套产品控制面；
9. Windows、macOS和Linux必须共享数据库合同，平台差异只出现在文件原子替换与权限实现；
10. 自动Vacuum可能长时间持有锁，不能进入Agent热路径或本切片隐式执行。

## 候选方案

| 方案 | 优点 | 主要问题 | 结论 |
|---|---|---|---|
| 每次启动按TTL直接删除 | 实现最少 | 无Dry Run、无恢复游标、启动路径有破坏性、无法审计 | 拒绝 |
| 只执行Artifact现有`collect` | 复用已有能力 | 不覆盖Session/Protocol增长，不能形成跨Store禁删集合 | 拒绝 |
| 新建独立Maintenance HTTP/Worker | 可后台调度 | 重新引入第二服务、认证与生命周期，违背单一产品拓扑 | 拒绝 |
| 导出到外部数据库后统一GC | 可扩展 | 增加远程依赖和迁移面，不能解决本地产品当前数据安全 | 拒绝 |
| 不持久化Dry Run，仅返回内存候选 | 接口简单 | 进程退出后候选消失，无法证明恢复没有重新选择 | 拒绝 |
| 不可变Plan + Item游标 + 每批同事务提交 | 可审计、可恢复、边界明确 | 增加Schema和内部记录 | 采用 |
| 精确解析每个`accepted`请求目标 | 保护范围小 | 账本故意不保存原始参数，摘要不可逆 | 当前不可行 |
| 有任意`accepted`时全局保护Session/Artifact | 保守、不误删 | 可能延迟不相关数据清理 | 采用 |
| 自动Checkpoint/Vacuum | 可缩小物理文件 | 可能阻塞交互且平台性能未定标 | 本切片拒绝，留给0.9.3d |

## 决策

### 1. 维护属于Session共库内部能力

新增`SQLiteStoreMaintenance`，由宿主在持有`SQLiteSessionStore.runtime_owner()`的维护窗口内显式调用。它组合Session、Protocol
Request和Artifact三类表，但不依赖App Server、模型、Trusted Action Executor或外部网络，不暴露公共Agent Protocol方法。

容量只读快照允许在已初始化数据库上调用；Plan、Execute和Restore要求活跃Runtime Owner。Runtime Owner阻止第二进程接管，
但不能阻止同一进程的其他协程继续发业务命令，因此运维合同进一步要求宿主先进入静默维护窗口。

### 2. 容量报告是低敏逻辑与物理双快照

`StoreCapacityReport`固定按`session → protocol_request → artifact`顺序包含三类`StoreCapacitySnapshot`，记录：

- Schema版本、数据库文件字节和WAL字节；
- 每类逻辑行计数、最早/最新时间、活动/终态/未知计数；
- Artifact正文总字节。

扫描同时校验Thread投影摘要和事件序号、Protocol终态摘要与时间顺序、Artifact时间字段。容量异常不是“统计缺失”，而是
`projection_corrupt/request_corrupt/artifact_corrupt`失败关闭。公开模型不携带业务ID、路径或正文。

### 3. 任何删除前先持久化不可变Plan

`RetentionPolicy`显式给出Cutoff、最多候选数和三类清理开关。规划在`BEGIN IMMEDIATE`事务中读取一致事实，建立禁删集合，
再写入：

- `store_maintenance_plans`：公开计划JSON及SHA-256；
- `store_maintenance_items`：有序内部候选、Key摘要和前置条件摘要；
- `store_maintenance_progress`：独立可变状态和下一序号。

公开`MaintenancePlan`只返回候选/保护计数和候选集合摘要，不返回内部ID。Plan和Item不可修改；任何Payload、Key、顺序、前置
条件或集合摘要不一致均报`maintenance_corrupt`。

### 4. 禁删判断失败时保守跳过

规划阶段保护：

- 未归档、活动Turn或保留期内的Thread；
- 包含Pending Approval、READY/RUNNING/UNKNOWN/RECONCILING等不确定效果的Thread；
- 作为其他Thread `fork_snapshot.source_thread_id`的来源Thread；
- 正文仍在保留期的Artifact所属Thread；
- 任意`accepted` Protocol Request存在时的全部Session与Artifact；
- `accepted` Request本身和保留期内的终态Request。

执行阶段重新加载候选并核对前置摘要、Cutoff和禁删集合。事实变化、记录已不存在、新增`accepted`请求或新Fork不会让Plan
失败重选，而是把对应Item计为`skipped_items`。跳过是安全结果，不得自动生成替代候选。

### 5. Artifact先清正文，Thread再删归属记录

Artifact Item把`published`改为`expired`并把`body`设为`NULL`，保留Manifest Tombstone。只有整个已归档Thread满足删除条件，且
它的所有正文均已过期后，Thread Item才能在一个事务中依次删除其Artifact Manifest、Event和Projection。

同一Thread的Artifact Body Item在计划顺序上位于Thread Item之前；若`max_items`不足以容纳完整组，则不选择该Thread，避免留下
“计划必然无法执行”的半组。独立Protocol终态记录可以在同一计划中按Cutoff删除。

### 6. 每批业务变更与进度游标原子提交

进度状态为`planned → running → completed`。执行以1～1000个Item为一批：

```text
BEGIN IMMEDIATE
  读取并验证Plan、全部Item与Progress
  从next_ordinal取有界批次
  对每个Item重新验证并apply或skip
  CAS更新next_ordinal/applied/skipped/completed_at
  核对Runtime Owner Token
COMMIT
```

因此事务提交前崩溃不会留下业务变更；提交后崩溃时游标已同时前移。恢复使用同一Plan ID和同一备份继续，不重新扫描候选。

### 7. 执行前必须创建Plan绑定备份

首次执行把完整数据库复制到调用方指定路径，验证Harnessix `application_id`、`quick_check`和计划Payload摘要，再通过临时文件、
`fsync`和原子替换发布备份。只有备份摘要写入Progress并把状态CAS为`running`后，才能执行第一批Item。

若进程在备份文件发布后、Progress转为`running`前退出，重试可复用同一Plan绑定且完整的备份；其他既有文件、不同Plan或摘要变化
均失败关闭。运行中的计划只能继续使用Progress记录的同一备份摘要。

### 8. 恢复是显式原子文件替换

`restore()`先验证备份，再Checkpoint当前WAL，把备份复制到同目录临时文件，复核SHA-256后原子替换数据库并删除旧WAL/SHM，
最后重新执行Session初始化和容量扫描。恢复源不能等于当前数据库。

恢复会把数据库还原到“Plan已创建、尚未转入running”的备份时点；不会合并维护后的新事实。生产恢复必须在没有同进程业务调用的
维护窗口执行。

### 9. Migration 26只增加维护元数据

Migration 26：

1. 为`agent_artifacts`增加`created_at`并建立索引；
2. 旧行无法恢复真实发布时间，保守用`expires_at`回填；清理资格仍以既有`expires_at`判断；
3. 新增Plan、Item、Progress三张`STRICT`表；
4. 不自动创建Plan、不自动清理、不自动Vacuum，也不改写Event或Thread JSON。

新Artifact写入统一经过[`artifacts/persistence.py`](../../src/harnessix/artifacts/persistence.py)的显式列清单，避免后续扩列破坏位置插入。

## 不变量

1. 用户升级或Agent启动不会自动删除任何业务记录；
2. Plan持久化提交前不会执行清理；
3. Plan和Item不可变，Progress是唯一可变维护记录；
4. `applied_items + skipped_items == next_ordinal <= total_items`；
5. `running`计划必须有已验证的备份摘要；
6. 每个业务变更与对应Progress推进在同一事务；
7. 活跃、未决、不确定、Fork来源或保留期内事实不删除；
8. 任意`accepted` Protocol Request全局保护Session和Artifact；
9. Artifact正文先Tombstone，Thread删除后才移除其Manifest；
10. 公开报告不包含业务身份、路径、正文或Secret；
11. Restore不做三方合并，只原子恢复完整备份；
12. Maintenance不执行Tool、模型请求、Action或外部效果；
13. 本切片不执行自动Checkpoint/Vacuum。

## 失败与恢复语义

| 故障 | 稳定结果 | 已提交事实 | 恢复动作 |
|---|---|---|---|
| 无Runtime Owner规划/执行/恢复 | `maintenance_runtime_required` | 不变 | 进入静默维护窗口后重试 |
| Plan/Item/Progress摘要或结构损坏 | `maintenance_corrupt` | 不继续写 | 从可信备份恢复并审计 |
| Schema在Plan后变化 | `maintenance_schema_changed` | 不启动执行 | 重新规划，不改旧Plan |
| 备份缺失/损坏/不同Plan | `maintenance_backup_missing/invalid/changed` | 不执行下一批 | 使用原备份或新Plan |
| 规划提交后崩溃 | Plan为`planned` | 业务事实不变 | 以Plan ID开始执行 |
| 备份发布后崩溃 | Plan仍为`planned` | 业务事实不变 | 复用经验证备份 |
| Progress转running后崩溃 | 有备份摘要 | 尚未执行Item | 以同备份继续 |
| 批次提交前崩溃 | 整批回滚 | 游标不前移 | 重跑同批 |
| 批次提交后崩溃 | 整批已提交 | 游标同步前移 | 从next_ordinal继续 |
| 候选被业务事实改变 | Item记为skip | 新事实保留 | 新Plan再评估 |
| 恢复复制中断 | 当前库未替换 | 当前库保持 | 删除临时文件后重试 |
| 恢复替换后进程退出 | 备份库已成为当前库 | 原子边界明确 | 正常initialize/检查 |

## 后果

### 正面后果

- Session、Protocol和Artifact增长第一次拥有同一低敏容量口径；
- 破坏性操作从“扫描即删除”变成可审阅、可校验、可恢复的Plan；
- 崩溃恢复不重新猜测候选，能证明零重复Item推进；
- 备份成为执行前硬门禁，而非运维建议；
- Protocol不可逆摘要造成的关联缺口被显式转化为保守禁删，而不是脑补目标；
- 不引入独立服务、消息队列或远程数据库，产品拓扑保持收敛。

### 负面后果

- 任意陈旧`accepted`记录会阻止全部Session/Artifact清理，容量释放可能不及时；
- 维护Plan自身持续增长，本切片不递归清理维护历史；
- 逻辑删除和Tombstone不保证SQLite物理文件立即缩小；
- Runtime Owner只能排除第二进程，宿主仍需保证同进程静默窗口；
- 旧Artifact的`created_at`只能保守回填为`expires_at`；
- Restore丢弃备份时点之后的全部数据库事实，必须作为完整回滚而非合并使用。

## 兼容、升级与回滚

- Migration 26只能向前追加；已发布Migration 1～25摘要不变；
- 新版本可以从旧25数据库原子升级，旧二进制不保证读取已写入26记录；
- 升级不触发Plan或清理；未调用Maintenance时业务行为只增加Artifact发布时间列写入；
- 代码回滚前若数据库已升级到26，应恢复升级前备份或使用认识26的Reader，禁止手工删除Migration行；
- 维护执行后的业务回滚使用该Plan绑定备份，不编写逆向SQL；
- 三平台文件替换、权限和WAL行为必须由CI及0.9.5安装升级矩阵继续验证。

## 验证要求

1. 容量报告固定包含三类Store且不泄漏Canary、路径或业务ID；
2. Plan必须要求Owner、限制候选数并检测Plan/Item篡改；
3. `accepted`请求必须全局保护Session和Artifact，但不阻止符合Cutoff的终态Request删除；
4. 备份发布后故障必须能复用同一备份；
5. 每批提交后故障必须从精确游标继续，最终applied/skipped守恒；
6. 候选前置条件变化或新增`accepted`请求必须跳过而非误删；
7. Restore后Thread、Artifact正文和Protocol记录必须恢复到备份状态；
8. Migration 26必须通过旧库升级、真进程退出、Checksum和WAL并发回归；
9. Ruff、Mypy、可读性、文档、规格和全仓测试必须通过；
10. Linux Python 3.12/3.13、macOS、Windows、固定Container和文档CI全部通过后，才能关闭0.9.3b。

## 未由本ADR解决

- Protocol `accepted`记录的业务目标索引和恢复裁决；
- 维护历史自身的保留与压缩；
- WAL Checkpoint、Incremental Vacuum、物理文件回收时延和性能阈值；
- 用户级数据导出、选择性删除、安全擦除和DLP；
- Trusted Action/Process Owner Fencing与跨Store孤儿；
- 定时调度、公共CLI/TUI维护命令和确认UX；
- 多租户远程数据库或在线并发Maintenance。

这些范围分别进入0.9.3c/d、0.9.4和0.9.5，不得用本ADR的内部API外推为已完成产品能力。
