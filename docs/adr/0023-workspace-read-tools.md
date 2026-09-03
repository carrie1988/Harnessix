# ADR 0023：工作区绑定与首批只读编码工具

- 日期：2026-09-03
- 状态：已实现并完成本地离线验收；对应 0.5.1

## 1. 顺序调整与复用

用户要求继续后续阶段。0.4.3c 的真实计价适用性仍独立待验收，不再阻塞没有模型消费的 0.5.1；不将此调整写成 0.4 已完成。本片只实现 `list_files` / `read_file`，不开放写、Shell、搜索或 Artifact。

复用现有 ToolRuntime、ToolDescriptor、ToolCall/Result、审批指纹和 Session v5。Action Plane 的 ToolRegistry 绑定 ActionExecutor，不为了本地读取伪造 ActionExecutor 或发往 Worker；CodingToolRuntime 维护固定的受信只读绑定表，不另建公共插件注册协议。

## 2. 授权与持久身份

宿主选择本地根目录和额外拒绝路径，模型只提交严格类型的相对路径。工具版本由实现版本、根规范路径/设备/inode、路径策略、输入/输出 Schema 和固定执行限制的摘要组成。既有 ToolCall.tool_version/tool_fingerprint 因此持久绑定工作区能力；重开相同根可继续审批，替换根/变更规则/切换工作区则旧调用失效。不修改旧 Schema 或在模型参数中接受权限。

目录 FD 逐级 `open(dir_fd=..., O_NOFOLLOW)`，预检查与 fstat 核对实际类型/身份；普通文件还使用 O_NONBLOCK 并拒绝多硬链接，避免 FIFO 阻塞和硬链接绕开名称策略。读取前后检查根、路径链与对象状态；观察到替换/变更则丢弃结果。不宣称拥有对恶意宿主的文件系统 CAS、挂载隔离或 OS Sandbox；管理员、inode 重用、同权限恶意修改者及网络/特殊文件系统不在该能力边界内。

默认拒绝任意层级 `.git`、`.harnessix`、`.codex`、`.ssh`、`.aws`、`.gnupg`、`.env` / `.env.*`、常见私钥文件名和私钥扩展名；比较使用 casefold。宿主额外拒绝路径按相对路径前缀匹配。列目录也隐藏这些名称；这不是完整 gitignore 或内容 DLP。Session 应放在根外或 `.harnessix` 内，其他私有目录须由宿主显式拒绝。

## 3. 输入、分页与输出

- 严格 Pydantic 输入，拒绝额外字段、字符串化数字、bool 假整数；路径最多 1024 UTF-8 字节、64 层，拒绝绝对路径、空段、`.`/`..`、反斜线、控制字符；目录根仅允许 `.`。
- `list_files(path='.', limit=100, offset=0, expected_revision=None)`：非递归，按名称排序，最多 200 项/页；最多扫描 10000 条/2 MiB 名称，超限失败而非返回伪完整排序。输出名称、类型、revision、next_offset；后续页必须传前页 revision，目录条目身份/类型变化使其失效。
- `read_file(path, start_line=1, max_lines=200, expected_revision=None)`：LF 分行，保留 CRLF；最多 2000 行/24 KiB 文本，单行最多 4 KiB，扫描最多 2 MiB。超大文件可读取有界前缀，但过深偏移/超长行拒绝。UTF-8 严格解码，拒绝 NUL/二进制控制字符，不自动转码。分页时必须携带 revision。
- 文件 revision 是根/路径/设备/inode/size/mtime/ctime 等观察事实的摘要，**不是内容哈希或原子快照**。只验证实际扫描片段的编码，不宣称未读尾部有效。
- 空文件成功返回空文本；越界行号失败。达到行数/字节上限显式标注截断原因和下一行，不截断半个 UTF-8 字符或悄悄跳过长行。
- 输出经绑定的 Schema 再验证；限制最终 JSON 大小。预期失败使用固定 `tool_*` 错误码，不返回绝对路径、OS 异常原文或无效参数原文；内部输出契约破坏抛 KernelError。

## 4. 执行、取消与恢复

本片保持顺序执行。同步本地文件 I/O 放入线程，逐条/逐块检查停止标志和 5 秒协作期限；Task 或 CancelToken 取消后设置停止标志，等待线程退出、FD 关闭再传播取消。不能用 to_thread 被取消来声称线程已经结束；卡在内核 I/O 时没有硬超时保证。

Kernel 继续负责审批、持久调用/结果和中断恢复。工具结果未提交时不自动重读，恢复为 INTERRUPTED；等待审批可在核对同一能力后显式继续。0.5.1 不改变当前只读恢复语义。

## 5. 验收矩阵

覆盖严格输入、权限/工具版本漂移、根和中间路径交换、链接/特殊文件、UTF-8/中文/空文件/长行/扫描预算、分页与修改、OS 错误脱敏、输出契约、Task/领域取消及 FD 回收。集成验证真实 SDK + 离线 HTTP、Kernel、实际临时文件、SQLite 重开/Replay、审批与真实进程中断；不依赖 API Key。macOS 本地与 Linux CI 分别验证，不将未测试平台标记支持。

接口求证：[Python os](https://docs.python.org/3.12/library/os.html#files-and-directories)、[asyncio shield/to_thread](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.shield)。参考机制见 [Tool Runtime 研究](../research/tool-runtime.md)，未复制参考项目实现。

本地结果：新增 **69 项**测试通过；`make check` 为 **889 passed、1 skipped**（未配置本地 PostgreSQL），Ruff/Mypy 通过。基础 wheel 在独立环境中确认没有 OpenAI/Anthropic SDK，仍可运行只读 Kernel 验收。异步调试和远端 CI 结果见 [测试验收记录](../testing-and-evals.md#14-051-只读编码工具验收2026-09-03)。CI 增加 macOS 只读套件，Linux 继续完整回归；远端运行结果以相应提交的 CI 为准。
