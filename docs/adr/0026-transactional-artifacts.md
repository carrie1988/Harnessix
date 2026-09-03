# ADR 0026：同一 Session 事务发布有界 Artifact

- 日期：2026-09-03
- 状态：0.5.2b2 已实现并通过本地验收；CI 结果以对应提交为准

## 决策与取舍

基线 `0c39c39` CI 全部通过。复用 SQLite Session 的单库事务、CAS 和宿主锁；不新增文件 blob 双写、远程数据库或后台队列。新增 migration 0006，不改旧 migration、Agent v5 或默认工具 Schema。旧程序遇到新 migration 必须拒绝打开，不能冒充可降级。

有界内容（最多 1 MiB）以 BLOB 保存，manifest、完整 ToolResult 的事件和投影在同一 BEGIN IMMEDIATE 事务提交。未提交即由 SQLite 回滚；提交后必有可关联结果。文件系统崩溃/刷盘依赖沿用现有 WAL/FULL 边界，不能据 SIGKILL 测试宣称覆盖所有断电/磁盘故障。[SQLite 事务](https://www.sqlite.org/atomiccommit.html)、[WAL](https://www.sqlite.org/wal.html)。

## 组合与信任

宿主显式创建 `SQLiteArtifactStore(session)`，同时传给 Kernel 和 CodingToolRuntime；Kernel 拒绝与 Session 对象不同的 Publisher；载荷绑定实际 Publisher 实例，防止生产器与读取器静默接入不同策略/存储。Scoped 工具可返回进程内 `ArtifactToolResult`，内容不通过模型参数提供；旧工具仍返回 ToolResultContent。Kernel 只允许 Scoped 入口提交此载荷，未配置 Publisher 则失败，不静默丢弃正文。

发布要求当前 Session 持有 Runtime 宿主锁对应的存活 token，并在提交前确认仍是同一个宿主。事务重新核对活跃 Thread/Turn、完整未完成调用和审批决定。历史 scope 不是发布租约。Artifact ID、归属、摘要、时间由宿主产生；实际 Workspace capability 摘要由受信 CodingToolRuntime 提供，不从路径标签或模型字段推断。一次调用最多一份 Artifact，不自动重发已完成调用。

模型只得到有界 preview 和 ArtifactRef（ID、SHA-256、字节/记录数、格式、完整性、过期时间），不包含内部数据库路径或归属控制字段。读取工具只接收 ID/分页参数，真实 Thread 和 Workspace 来自已验证 scope/运行时；跨 Thread（即使同根）或重开绑定到不同根身份/工作区拒绝策略返回相同 not_found。已打开的 Workspace 持有原 capability；读取归档只查私有数据库，不重新读取源文件，也不声称每次检查根路径现状。Artifact 存储策略改变会使待审批搜索契约失效，但不是对已归档内容重新做来源路径授权。宿主存储 API 的身份参数属于受信接口，不是新的网络鉴权服务。

## 搜索、完整性与兼容

Artifact 模式下 glob/grep 在原扫描/权限预算内继续收集预览以外的记录；每条记录为 UTF-8 JSONL。最多 10000 记录/1 MiB，超限则整个工具失败，不发布伪完整前缀。原模式仍在预览截断后停止；默认工具版本/输出 Schema 不变。

Artifact 模式的搜索工具版本额外绑定输出封装、捕获预算和存储策略；启用/关闭或修改策略使旧搜索审批失效。list/read 不变。片段裁剪和非法编码/超大文件等缺口仍明确保留；Artifact.complete 表示当前定义范围是否全部扫描，不表示工作区原子快照或每条片段都是完整源码行。

## 生命周期与配额

正文不可变；SHA/长度/记录数及 manifest/结果引用核对失败返回 corrupt。默认 TTL 24 小时，最多 7 天；读取过期对象返回 expired，缺失与损坏不冒充空页。分页按完整 JSONL 记录且有输出字节上限，不截断半个 Unicode 字符。

发布事务限制单件 1 MiB、单 Turn 累计 4 MiB/128 件、全局保留正文 32 MiB/10000 份 manifest；采用逻辑内容/元数据配额，不宣称限制整个 Session 数据库、WAL 或历史事件的物理大小。清理释放正文保留过期 tombstone，SQLite 可能复用空闲页而不是立即缩小文件；物理磁盘满沿用 storage_full，不能假报保存成功。

清理为宿主显式、有界事务；返回 `next_after`，宿主沿游标继续，避免受保护前缀饿死后续待清理对象。任何活跃 Turn 的所属 Thread 均保守保护，不删除其正文。过期读取不延长 TTL。tombstone 保留且计入 manifest 上限，不自动删除历史；达到累计上限时需由宿主按保留策略轮换 Session，当前没有透明无限归档能力。未提交 Artifact 不产生持久孤儿；提交后/终态前崩溃仍有结果引用，恢复为 INTERRUPTED 而不重复搜索。篡改数据库造成无引用记录属于损坏，不当作合法待恢复业务状态。Replay 重建事件/投影，不重建已过期或丢失的正文。Task 取消在提交前回滚、提交后保留成对事实；用户取消与发布竞争同一 Thread 锁，允许先完成原子提交再进入取消终态。

## 验收

验证真实搜索预览之外的记录、同库发布、跨会话/工作区拒绝、过期/缺失/损坏、容量边界与并发配额、取消、发布各切点崩溃、旧数据库升级/Schema 冻结、独立 wheel、离线 SDK、Replay 与跨平台 CI。不调用真实模型，不把本片当作已完成写工具或自主编码 Eval。
