---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [artifacts, agent]
related_adrs:
  - docs/adr/0163-artifact-archives-are-optional-when-storage-is-full.md
related_tests:
  - tests/artifacts/test_store_capacity.py
  - tests/artifacts/test_reclaim.py
supersedes: []
---

# ADR 0164 过期Artifact正文自动回收，Tombstone不再占全局件数

- 状态：评审中
- 日期：2026-11-15
- 修订：Artifact模块文档“两条路径都不自动调度”的既定设计；全局件数配额的计数口径

## 背景

Artifact是命令输出、搜索结果与审批Review的完整存档，与Session同库（每个工作区一个`sessions.db`）。存储总量有两项
全局配额：`max_live_bytes`（默认32 MiB，表中非空正文的字节和）与`max_manifests`（默认10000，表中全部行数，含已回收
的Tombstone）。每份存档有24小时TTL，到期后不可读，但正文只有`SQLiteArtifactStore.collect()`才会清空，而产品里从未调用
它（文档写明“不自动调度”）。于是：

1. 过期正文永远留在库里并继续计入`max_live_bytes`。同一工作区累计32 MiB正文后，此后每次归档都撞全局配额：只读
   搜索使Turn失败，命令输出使Turn以`uncertain_effect`中断（见ADR 0163）。
2. 即使回收了，Tombstone仍计入`max_manifests`。累计10000份后同样每次都撞线，只能删除状态目录恢复。

长任务每步约产生0.8～0.9份存档，所有Beta用户用得越久越容易碰上。2026-11-15 NL2RepoBench冒烟中两个用例在第143、156
步被单Turn配额中断（0.10.0b46已把单Turn默认值放到上限），定位时发现了这两颗“定时炸弹”。负责人2026-11-15批准自动回收
（与ADR 0163的降级一起）。

## 决策

1. **自动回收。** `artifacts.reclaim.ArtifactReclaimer`在`AgentRuntime`打开时（恢复开放Turn之后）回收一轮，最多占用
   2秒，积压更多时剩余部分立即转入后台；此后每小时回收一轮，Runtime关闭时取消。每批50行、各自一个写事务，批间让出
   事件循环。任何失败（如数据库忙）只记一条不含正文的警告日志，等下一轮，不影响宿主与正在运行的Turn。只有提供
   `collect`的存储才调度（`ArtifactCollector`协议），测试替身不受影响。
2. **回收规则不变。** 仍是`collect`：只处理到期的Published行；所属Thread有活跃Turn时保护；清空正文、保留Tombstone，
   历史引用照旧明确失败为`artifact_expired`；不删除Manifest，不运行Vacuum。
3. **坏行不卡住游标。** 归属Thread不存在、Manifest或Session引用无法核对（`artifact_unreferenced`以外的错误）的行不再
   中止整批（此前整批回滚，下一轮从同一位置再失败，之后的行永远回收不了），而是保留正文、计入`protected`并继续。
   同一批里同一Thread的快照只读取一次。
4. **Tombstone不占全局件数。** `max_manifests`只计Published行，回收后的Tombstone不再占名额。单Turn件数与字节仍计入该
   Turn的全部存档（含已回收的），约束一个Turn的产出。

## 没有改变的

- 配额默认值与取值范围、Artifact Policy Schema（`artifact-policy-v2`）、TTL与读取语义。
- 共库维护（`SQLiteStoreMaintenance`）仍是显式、Plan-first的离线路径；自动回收不删除Manifest、不随Thread删除行。

## 后果

- 正文占用由“只增不减”变为“最多约24小时的产出加一小时回收间隔”；`max_live_bytes`在正常使用中不再被历史正文耗尽。
- Tombstone行仍会累积（每行只有Manifest与少量元数据），数据库文件不会因回收而缩小（SQLite释放页留在文件内）。行数
  增长与文件大小留给共库维护与后续容量告警。
- 回收与宿主写入竞争同一写锁：批次小、批间让出，单批通常在数十毫秒内完成，远小于5秒的忙等待上限。
- 三个平台行为相同：回收与配额只依赖Python与SQLite，没有平台分支；时间判断沿用UTC墙钟。

## 验证

- `tests/artifacts/test_store_capacity.py`：回收后的Tombstone不再挡住新归档（份数上限2）；默认10000份Tombstone后新会话
  首次归档成功且带存档；默认32 MiB过期正文在下次打开Runtime时被回收，新归档成功。三项在0.10.0b46上均失败
  （`artifact_quota_exceeded`，Turn失败）。
- `tests/artifacts/test_reclaim.py`：运行期间到期的正文被定期回收；回收失败只记日志并在下一轮重试；启动积压超出预算
  时转入后台继续；关闭时取消；无法核对的行被保留且不挡住之后的行（0.10.0b46上`collect`整批失败）。
