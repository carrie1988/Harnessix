# ADR 0024：有界搜索与 Artifact 归属拆片

- 日期：2026-09-03
- 状态：0.5.2a 与 0.5.2b1 上下文已实现；0.5.2b2 Artifact 待实施，整体 0.5.2 未完成

## 1. 拆片与实际接口约束

0.5.1 已通过 Linux Python 3.12/3.13、macOS 和 PostgreSQL CI。本次先交付可独立验收的文件定位/内容搜索，不把搜索与 Artifact 一次性塞进现有只读接口。

0.5.2a 时的 `ToolRuntime.execute(call, cancel)` 没有可信 Thread/Turn 上下文。将 Thread ID 放进模型参数不能证明归属，把 Artifact 简单绑定到 Workspace 又会混淆同根下的多个会话。因此先设计宿主注入的执行作用域、兼容旧 ToolRuntime 的调用方式，再实现 Artifact 持久化与读取/回收。后续 0.5.2b1 已落地显式 Scoped 入口，见 [ADR 0025](0025-trusted-tool-execution-scope.md)；Artifact 仍未实现，当前不新增伪归属字段或共享全局输出目录。

## 2. 组件求证与选择

- 已阅读冻结 OpenCode 的 `grep.ts`，它经受信执行上下文取得 session/tool-call 身份并委托 Ripgrep。参考边界，不复制实现。
- 本机 `rg --version` 为 15.2.0。Ripgrep 是未来受控进程搜索的候选，但直接将根路径交给外部进程，不能自动继承 0.5.1 的逐段 FD/no-follow 与名称策略；本片不提前引入尚未实现的 Process Runtime。
- [Python fnmatch](https://docs.python.org/3.12/library/fnmatch.html) 支持逐名称通配；本片复用 `fnmatchcase` 进行大小写敏感的单段匹配，用有界状态表处理完整 `**` 路径段，不实现正则引擎。
- [Python glob](https://docs.python.org/3.12/library/glob.html) 的递归路径展开可涉及目录链接，结果顺序和并发变更也不是稳定快照。不能仅因它支持 `dir_fd` 就将其直接当作工作区权限边界。
- `grep` 首片只支持大小写敏感的字面量查询，使用标准字符串查找；不接受任意正则，不做 PCRE/子进程或全文索引。后续正则支持须有独立的时间/复杂度与取消证据。

## 3. 已定契约

`glob(path='.', pattern='**/*', max_results=100, include_ignored=False)`：path 是相对目录；匹配相对于它的完整路径，但结果返回相对于 Workspace 的路径。支持单段 `*` / `?` / `[]` 与完整段 `**`（零或多级）；不扩展波浪线、环境变量、花括号或正则。模式最多 256 UTF-8 字节、32 段，禁止空段、`.`/`..`、反斜线和控制字符。点开头的普通名称参与匹配，不冒充 Shell 默认隐藏规则。

`grep(path='.', query, include='**/*', max_results=100, include_ignored=False)`：只搜索目录下匹配 include 的文件；query 最多 256 UTF-8 字节、非空、无控制字符/换行。每个命中行返回一条记录，包含相对路径、1 起始行号、包含首次命中的有界原文片段、片段是否裁剪及文件 revision；此 revision 可直接传给 read_file 的 expected_revision，不是内容哈希。

两工具最多返回 200 项。先有界发现候选，再按完整相对路径排序；grep 同文件按行号排序。达到结果数量或 24 KiB 记录预算时返回显式 truncated/reason，scan_complete=false；没有搜索分页游标或自动保留的完整输出，调用方应缩小路径/模式。

## 4. 权限、忽略与扫描预算

- 复用 Workspace.parts/open、对象身份/变化检查、只读版本/审批绑定、线程停止与关闭回收。模型参数不能放宽工作区拒绝策略。
- 遍历不跟随符号链接，忽略特殊文件、多硬链接以及工作区拒绝名称/路径。由目录枚举发现的对象在实际打开后再次比对身份，发生替换则失败，不返回部分成功。
- 默认剪枝目录：node_modules、.venv、venv、__pycache__、.pytest_cache、.mypy_cache、.ruff_cache、dist、build。include_ignored 只放宽这些性能忽略，不放宽 .git/.env 等权限规则；显式选择的搜索起点不因性能忽略而拒绝。**不解析 .gitignore 或全局 Git 配置**。
- 每次最多枚举 10000 项、2 MiB 名称、32 级相对深度，协作期限沿用 5 秒。超限/超时返回固定工具失败，不将任意遍历前缀包装成完整排序结果。
- grep 每个文件最多 2 MiB、总读取最多 16 MiB，整文件严格验证 UTF-8/二进制控制字符后搜索；单行超过 4 KiB 跳过该行并报告不完整。超大/非法编码/二进制/不可读对象计数明确保留；有这些跳过时 scan_complete=false，空命中不能解释为全目录不存在。
- 单条片段最多 384 UTF-8 字节，在字符边界截断；不得伪装为完整行。结果 JSON 仍受现有 60000 字节上限约束。
- 搜索不是全树原子快照。完整仅表示本次定义的范围被扫描，后续读取仍必须校验 revision；对未知/异常 I/O 不猜测成功。内核 I/O 阻塞仍无硬超时保证。

## 5. 版本与恢复

新增独立 glob/grep 输入输出 v1 Schema，不改写 list/read v1、Agent v5 或 Action v1。新搜索工具的版本绑定搜索预算、忽略策略与语义；旧 list/read 的版本保持原样，新增广告工具不使其已持久审批失效。

搜索结果仍是现有 ToolResult.output，失败/取消/进程中断复用 Kernel 语义：结果未持久化时不自动重搜。0.5.2a 没有后台子进程、额外线程调度器、Artifact 落盘或真实模型调用。

## 6. 0.5.2b 准入与验收计划

先固化受信 Execution Scope（Thread、Turn、Call、Workspace 能力）与旧端口兼容 ADR；不可依赖模型输出中的归属 ID。Artifact 应是私有不可变输出、有独立 manifest/摘要/大小/状态/过期时间，并验证会话归属、磁盘配额、原子发布、取消与未提交 ToolResult 的孤儿恢复。清理不能删除活跃引用，过期/缺失/损坏不冒充空输出。上述能力完成前，0.5.2 整体不勾选通过。

0.5.2a 测试要求：通配语义与严格类型、越界/链接/替换、忽略与权限分离、确定性排序、编码/大文件/长行、数量/字节/枚举上限、取消和错误脱敏、搜索→读取 revision、真实 SDK 离线调用、持久审批/Replay 与真实进程中断不重搜。最后运行 make check、异步调试、独立基础 wheel 和跨平台 CI。
