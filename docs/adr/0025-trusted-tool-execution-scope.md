# ADR 0025：可信工具执行作用域与旧端口兼容

- 日期：2026-09-03
- 状态：0.5.2b1 已实现；Artifact 存储/发布/清理属于 0.5.2b2，尚未实现

## 1. 证据与边界

基线 `9a70ce4` 的 Linux Python 3.12/3.13、macOS、PostgreSQL CI 已全部通过。当前工具只收到 call/cancel，Thread/Turn 仅在 Kernel 内可见。新增可信上下文是 Artifact 归属的前置条件，但不等于已解决跨持久介质提交。

已核对冻结源码：Codex `a0dcfe2` 的 `core/src/tools/router.rs` 在受信 dispatch 中用 Session/StepContext 构造 ToolInvocation，模型 payload 单独传入；OpenCode `69c172e` 的 `packages/core/src/tool/tool.ts` 将含 sessionID/toolCallID 的 Context 与工具 input 分开。本项目复用这一边界原则，不复制实现或引入这些项目的运行依赖。

## 2. 加法端口，不猜测签名

- 保留 `ToolRuntime.definitions()/execute(call, cancel)` 原样，原 `AgentRuntime(store, provider, tools)` 调用继续生效。
- 新增 `ScopedToolRuntime.definitions()/execute_scoped(call, scope, cancel)`，宿主通过 `AgentRuntime(..., scoped_tools=...)` 显式选择。
- 两个入口互斥，只从选中的一个入口获取定义；不维护合并 Registry，不按 hasattr/反射自动升级，不捕获 TypeError 后降级重试。内部 TypeError 只能失败一次。
- CodingToolRuntime 支持两个入口；旧入口不悄悄增加模型参数、审批权限或持久字段。新入口额外校验上下文及工作区一致性，复用同一只读执行路径。

## 3. 作用域来源与绑定

`ToolExecutionScope` 是 frozen/slots 的宿主进程内值对象，包含 thread_id、turn_id、call_id、workspace 和 request_fingerprint。它不是 Tool Input、Provider Event 或 Session Schema，不序列化给模型，也不新增 Session Migration。

Kernel 在审批通过/消费等待状态、工具版本/Effect Class 校验后，从最新 Session 投影构造作用域。构造必须满足：该 Turn 是 Thread 当前活跃 Turn、状态为 EXECUTING_TOOLS、call 与未完成的持久调用完全一致。拒绝审批、未知工具和未开放的写工具不进入 scoped execute。

request_fingerprint 复用既有审批摘要算法，绑定 policy、Thread、Turn、Workspace 和完整 call（含参数、工具版本/指纹）。只提取公共的纯计算函数，不改旧摘要内容。`validate_call` 检查 scope/call 对应关系，防止误传另一 Call 或执行前参数漂移。

**信任声明**：这是 Kernel 到受信工具的显式上下文，不是密码学授权令牌，也不能约束同进程恶意 Python 插件。宿主仍拥有工具实现和配置权限；不得将公开构造一个 dataclass 当成取得授权。

## 4. Workspace 标签不等于文件系统能力

通用 scope.workspace 来自宿主持久 Thread；它本身不是目录授权。CodingToolRuntime 的 scoped 入口要求它严格等于运行时已规范化的 `workspace_root`，再由既有工具版本/指纹及 Workspace.open 检查真实根身份、拒绝策略和对象变化。

宿主应使用 `str(tools.workspace_root)` 创建 Thread。不在事件循环里重新 resolve 任意上下文路径，不接受不同路径拼写/符号链接别名来冒充相同作用域。旧入口保持原语义；新入口不匹配时以固定 tool_workspace_mismatch 失败，且未触碰目标文件。Artifact 后续必须记录运行时实际的 Workspace capability 摘要，不能只保存这个路径标签。

作用域不使用全局变量或 ContextVar，跨 Thread 并发、同 Thread 连续 Turn 和多个 Call 显式传参。取消仍由既有 CancelToken/Kernel/工具资源回收负责；上下文不是生命周期租约，不能在 Turn 结束后据其单独发布 Artifact。

## 5. Artifact 下一片的事务门禁

当前 ToolResult 由 Session.append 原子写入事件/投影。单独把一个输出文件 rename 成功，不能证明 ToolResult 已提交；失败和崩溃之间会形成孤儿输出。SQLite 的事务保证针对事务内变更，不自动覆盖外部文件；WAL 模式下也不能假设跨多个附加数据库整体原子。[SQLite 原子提交](https://www.sqlite.org/atomiccommit.html)、[WAL 文档](https://www.sqlite.org/wal.html)。

0.5.2b2 必须先选择并验证：有界内容/manifest 与 ToolResult 在同一 Session 事务发布，或独立 blob 的 prepare→发布引用→核对/回收协议。不能用“先写文件，再写数据库”声称二者天然原子。优先评估复用本地 SQLite，不为此部署远程中间件。

必须覆盖：宿主生成 ID、Thread/Turn/Call/实际 Workspace capability 归属、不可变内容、摘要/长度/类型、单件/单 Turn/总量配额、活跃引用保护、过期 tombstone、缺失/损坏/未提交区分、取消和结果提交前后崩溃、恢复时无自动重复工具效果。若需要新持久契约则新增 Schema/迁移，不改写旧版本。

## 6. 本片验收

- 旧端口回归及旧独立 wheel 待审批会话跨版本继续；所有旧 Schema/工具定义不变。
- Scoped 路由互斥、来源/参数绑定、不可变性、并发隔离、连续 Turn、多 Call；不存在 TypeError 降级或双执行。
- 实际 SDK 离线搜索链路、严格工作区绑定、审批等待/拒绝/恢复、读取中的取消和进程崩溃恢复。
- make check、异步调试、基础 wheel、Linux/macOS CI；不调用真实模型。0.5.2b1 单片不代表 0.5.2b/0.5.2 完成；后续 b2 已完成，见 [ADR 0026](0026-transactional-artifacts.md)。
