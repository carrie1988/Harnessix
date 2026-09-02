# ADR 0013：Kernel 语义契约、诊断与存储验收

- 日期：2026-09-03
- 状态：已接受并实现
- 范围：0.3.3；不包含真实模型、写工具和自动 Context Compaction

## 1. 语义 Item

补齐 ADR 0006 的 plan、context_compaction、error。

- Plan 是不可变的步骤快照，步骤 ID 唯一，最多一个进行中步骤；通过新 Item 的 supersedes 引用最新完成的 Plan，而不是改写旧事实。
- Compaction 是不可变的摘要事实，引用同一 Thread 中已终结的旧 Turn 的完成消息/工具 Item；工具调用与结果必须成对包含，不能引用当前运行中的调用。
- Plan/Compaction 只能在 PREPARING_CONTEXT 开始，开始和终值之间不改变内容；本阶段由可信宿主提交事件验证记录能力，自动规划、压缩算法和 Model View 替换在后续阶段实现。
- Error Item 记录结构化失败。新版本非成功 Turn 的终态与对应 Error Item 在一个事务提交；启动恢复保留旧错误事实，并追加恢复结论。
- Approval、Plan、Compaction、Error 属于控制面事实，不直接作为 Provider History 输入。

## 2. 错误契约

AgentFailure 复用既有 code/message/retryable，新增稳定、低基数 category。KernelError 提供统一转换方法；Provider 的 retryable 声明作为诊断信息保留，不触发自动请求重试或 Tool 重放。

SQLite/OSError 在存储边界归一化为公开 KernelError；不持久化底层异常原文。损坏事件或快照不能仅以裸 JSON/Pydantic 异常向上传播。

允许重试的错误不等于允许重放副作用。存储提交结果不确定时，仍须读取既有事实、使用 Event ID 或请求 ID 幂等处理。

## 3. 可观测性

复用 Observability/NoOp/OpenTelemetry 端口，默认不启用外部导出器。

- 每次 run/resume 是一个有限时长的 Turn 执行片段；审批暂停返回即结束 Span，重启后从持久 TraceContext 建立新片段。
- 覆盖 Model、Tool、审批答复、取消、启动恢复；终态指标只在本进程确认新的终态提交后累计。
- 指标标签仅包含操作、受控状态和错误类别；Thread/Turn/Call ID 只用于 Trace。
- 不导出 prompt、Workspace、工具参数/结果、审批 actor/reason、任意第三方异常原文或堆栈。
- Kernel 的安全包装层不把业务异常传入第三方 Span 的 exit；错误仅写入受控分类。OTel 故障不能改变业务结果、掩盖原始异常或触发重新执行。
- 外部导出器由宿主创建/关闭；Runtime 不擅自关闭共享导出器。
- 指标是尽力而为的运行诊断，不是 exactly-once 审计或计费；崩溃后以 Event Log 为准。

## 4. 存储与 Schema

- Agent Event/Thread 新写 v3，保留 v1/v2 Schema 和旧事件导出形式；不改写历史 JSON。
- Migration 0003 只推进最低读者版本，不改变物理表结构，防止旧程序对不认识的新 Item 做局部接管。
- 旧快照仍可读；新写/重建投影版本为 3。
- 抽取基于 SessionStore 端口的共享套件，SQLite-specific 测试继续负责迁移、宿主锁和物理损坏。
- 增加事件缺口/索引错配/坏 JSON、不可写/磁盘满、取消提交与重放不变量验证。
- 首版仍为本地单宿主、聚合快照；本阶段不增加其他数据库实现。

## 5. 验收门禁

1. 新 Item 正常、失败、取消、重放与跨版本兼容；
2. Provider 错误和 retryable 保真，原始异常不进入持久化错误/Telemetry；
3. 真实内存 OTel Exporter 验证父子关联、片段结束、低基数指标与敏感信息隔离；
4. 导出器故障不改变完成、失败、取消或原异常；
5. SessionStore 共享契约与 SQLite 故障测试；
6. make check、异步调试测试、离线示例、构建产物通过；
7. 文档明确当前能力与后续规划，满足后才将 0.3 标记完成。

## 6. 验收入口与限制

- tests/agent/test_semantic_items.py：不可变修订、来源/配对校验、错误事实、失败/取消与 Replay；
- tests/agent/test_semantic_crash_recovery.py：9 个语义提交进程崩溃边界；
- tests/agent/test_telemetry.py：真实 OTel 内存导出、跨重启关联、内容隔离与异常降级；
- tests/contracts/session.py：可复用 SessionStore 行为契约；
- tests/agent/test_storage_failures.py：实际 SQLite 只读/磁盘满及物理损坏；
- tests/agent/test_session_upgrade.py：原始 v1/v2 Transcript 升级与旧格式导出。

当前低基数指标与有限时长 Trace 覆盖 Kernel 操作，不是端到端通用 Redactor，也不控制第三方导出器自行产生的日志。聚合快照、长历史性能、真实 Provider 和硬 Sandbox 继续按后续里程碑验收。
