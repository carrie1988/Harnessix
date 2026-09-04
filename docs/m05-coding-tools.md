# 0.5 Coding Tool Runtime 详细实施设计

- 日期：2026-09-05
- 状态：0.5.1/0.5.2及0.5.3范围已交付；0.5.4a宿主进程基础层、0.5.4b1 Action Plane持久准入及0.5.4b2b1稳定Agent/Action身份已实现，b2b2事件迁移、b2c运行时、0.5.4c Git与测试工具及0.5.5 Eval待实施
- 目标：从“模型调用正确”推进到“能够在真实仓库中可靠定位、修改、验证并交付”

## 1. 实际基线与不扩大的边界

当前 Kernel 已有 `ToolRuntime.definitions/execute`、`ToolDescriptor`、持久 ToolCall/ToolResult、工具版本/指纹、审批和恢复语义。`ToolCallContent` 已包含版本、Effect Class、参数及审批绑定；`ToolResultContent` 已有 succeeded/failed/cancelled/unknown 和 Action ID。不要重新命名或并行建设另一套 Call/Result。

默认 `_execute_tool` 仍拒绝非 READ_ONLY；0.5.3仅通过显式 `patches` / `patch_batches` 专用端口接入受管单文件/整组写入和核对。不删除通用门禁，不让任意写工具或 Shell 获得执行权限。写审批、效果证据和双账本恢复见第 20 节。

本阶段不建设新 Agent Loop、TUI、向量库、分布式队列、Web 产品或长期记忆。0.1 Action Plane 不改成每个文件读取都经过 HTTP/Worker 的旁路。0.6/0.7/0.8 的规划不能被写成已经具备的能力。

## 2. 源码证据与本项目决策

研究仍使用 [冻结基线](research/baselines.md)，本次核对本地 HEAD 与基线相同：Codex `a0dcfe2`、OpenCode `69c172e`。参考机制，不复制实现。

- **事实**：Codex Registry 按工具声明查询并行能力；OpenCode Read Filesystem 同时限制行数、字节、长行，严格处理 UTF-8，并显式返回分页/截断。
- **决策**：能力来自受信 Registry，不来自模型参数；返回内容要明确读到了哪一段，不能把截断当作完整文件。
- **决策**：先保持 Kernel 顺序调度，真实只读链路稳定后才增加有界并发；不以 asyncio.gather 一次性并发所有调用。
- **决策**：路径校验不能只做字符串前缀或先 realpath 再直接 open。普通文件读写以 Workspace 能力和实际打开对象为边界；Shell 不能继承“只读工具”的安全声明。

具体索引见 [Tool Runtime 研究](research/tool-runtime.md)。已有研究中新增 PURE/LOCAL_WRITE 等概念是设计意图，不是当前 Action Contract 枚举；本阶段不原地扩展冻结的 Action v1。

## 3. 分片与准入条件

| 切片 | 交付 | 准入/完成条件 |
| --- | --- | --- |
| 0.5.1 | Workspace、工具绑定、list_files/read_file | 已实现；独立离线验收，0.4.3c 计价保留待办，不再作为本片前置阻塞 |
| 0.5.2a | glob/字面量 grep | 已实现；有界遍历、忽略/权限分离、输出上限、审批、revision 读取与中断不重搜 |
| 0.5.2b1 | 可信执行作用域、旧端口兼容 | 已实现；明确注入调用归属、校验工作区；不新增 Artifact 或改变旧持久契约 |
| 0.5.2b2 | 输出 Artifact | 已实现；同一 Session 事务发布、归属/配额/分页/清理、取消及 14 个进程崩溃切点通过 |
| 0.5.3a | 只读 Patch 计划准备 | 已实现；完整镜像、唯一精确编辑、来源/计划复核，不执行写入 |
| 0.5.3b1 | 受管单文件执行后端 | 已实现；私有副本、持久意图/审批、实际写与崩溃核对，宿主 API |
| 0.5.3b2 | Kernel 模型写工具闭环 | 已实现；Agent v6/migration 7、独立写审批、专用准入、SDK 离线闭环与双账本恢复 |
| 0.5.3c | 多文件效果与 Diff | 已实现整组准备/顺序效果、Kernel持久审批、双SDK离线闭环与计划/效果Artifact；不假报整体原子 |
| 0.5.4 | Process、Git、run_tests、受控 Shell | a宿主进程基础层已实现；b持久准入/死亡处理与c工具接入待实施 |
| 0.5.5 | 真实编码任务 Eval | 在非示例仓库完成受控缺陷修复，实际 Diff/测试/最终报告一致 |

这些是实现顺序，不是发布为生产可用的自动批准。写/Shell 在对应分片门禁前不出现在模型可见清单中，执行时仍再次检查；安全隔离能力不足的模式不能默认启用。

## 4. 模块与复用

计划在 `src/harnessix/tools/` 下按真实需要逐步建立：

~~~text
contracts.py       受信工具绑定、输入/输出模型与执行限制
runtime.py         实现现有 ToolRuntime；不另建 Agent Loop
workspace.py       Workspace 身份、路径分解、实际对象边界
files.py           目录分页与有界文本读取
search.py          搜索适配及明确的忽略/资源限制
search_contracts.py 搜索输入/输出、完整性与预算
patterns.py        fnmatchcase 单段通配 + 有界 globstar 状态表
../artifacts/      有界 JSONL、manifest、同库发布与分页/清理
../patches/        已有精确计划、受管副本、持久写执行及专用 Kernel 桥接
../processes/      argv、公开协议双流捕获、进程组、取消与直接子进程回收
~~~

不预建空壳文件。0.5.1 已有前四项，0.5.2a 新增三个搜索模块，0.5.2b2 新增 artifacts 包；0.5.3a 新增 patches/contracts.py 与 planner.py。0.5.3b1 新增 managed/managed_io/ledger/managed_contracts；b2 新增 agent_bridge/bridge_contracts 与 Kernel patching，0.5.4a新增processes/contracts/capture/runtime宿主基础层，不注册模型工具。

输入输出采用 Pydantic 严格模型，JSON Schema 从模型生成；复用 `ToolDescriptor` 给 Provider 广告。输出 Schema、权限和并发元数据保留在受信绑定中，不能由模型上送。影响执行/审批的元数据必须进入版本/指纹契约；若需要新增持久字段，单独升级 Agent Schema 与迁移，不能修改旧 Schema。

## 5. 首批只读工具契约

`list_files` / `read_file` 的已实现参数以 [ADR 0023](adr/0023-workspace-read-tools.md) 和生成 Schema 为准；`glob` / `grep` 以 [ADR 0024](adr/0024-bounded-search-and-artifact-scope.md) 和独立 v1 Schema 为准：

| 工具 | 参数要点 | 结果要点 |
| --- | --- | --- |
| list_files | 相对目录、页大小、受校验的继续位置 | 相对路径、条目类型、truncated、下一位置 |
| read_file | 相对文件路径、1 起始行号、行数上限 | 文本、实际行区间、UTF-8 字节数、截断原因、下一位置 |
| glob | 相对搜索根、模式、结果上限 | 排序后的匹配路径、遍历是否完整、截断原因 |
| grep | 相对目录、include 通配、大小写敏感字面量 query | 文件/行号/有界片段、revision、计数与完整性/截断信息 |

- 0.5.1 实际限制：单页最多 24 KiB 文本、目录最多 200 项；读取最多 2000 行、单行最多 4 KiB、扫描最多 2 MiB（文件缓冲最多额外预读 8 KiB）；最终输出 JSON 最多 60000 字节。
- 字节上限针对 UTF-8 编码，不以字符数冒充；中文、组合字符和跨块 UTF-8 均测试。
- 拒绝二进制/非法 UTF-8，不自动转码或返回替换字符伪装原文。
- 不把片段 SHA 当作完整文件 SHA；完整前镜像只在 Patch 准备阶段按独立上限获取。
- 分页不是一致性快照。目录/文件变更时返回明确失效/重读提示，不隐式跳过或重复结果。
- 行偏移很大、深目录、超多小文件和超长正则也必须有扫描/时间/结果预算，不能只限制最终输出。
- 0.5.2a 复用标准库 fnmatchcase，不增加外部进程或正则引擎。不宣称固定排除规则等价于完整 gitignore。

## 6. Workspace 与路径能力

Workspace 由宿主选择，模型只提交相对路径。默认不跨根，不访问 `.git` 内部状态、会话/Artifact 私有目录及被宿主敏感路径规则拒绝的文件。

1. 拒绝绝对路径、NUL、父目录逃逸和无效路径；不使用 startswith 判断目录归属。
2. 先明确符号链接策略。首片采用保守拒绝，不自动跟随目录或最终文件符号链接；未来放宽必须有额外测试与权限表达。
3. macOS/Linux 实现核对目录 FD、逐级打开及 no-follow 支持；检查最终对象是普通文件/目录，拒绝 FIFO、设备、socket，避免假“文件读”永久阻塞。
4. 打开后的对象/根身份要与授权对象一致；覆盖校验与使用之间替换目录、根被改名、链接交换等竞争测试。跨重开需要的身份绑定不能只存在于内存。
5. 受信路径能力限制本工具能访问的对象，不等于 OS Sandbox，也不保证普通 Shell 无法逃逸。
6. 默认只承诺已测试的本地文件系统模式；网络文件系统/特殊挂载的 I/O 硬超时能力另行评估，不将取消协程当作终止内核 I/O。

## 7. 输出与 Artifact

**0.5.2b2 已实现有界搜索 Artifact。** 默认搜索仍只有预览；宿主同时为 Kernel 与 Scoped CodingToolRuntime 配置同一 `SQLiteArtifactStore(session)` 后，才启用 JSONL 捕获与 `read_artifact`。正文、manifest、引用结果的事件/投影同库原子提交，没有外部 blob 文件双写。发布还校验活跃宿主、最新调用及审批，不接受历史 scope 作为持续租约。详细契约见 [ADR 0026](adr/0026-transactional-artifacts.md)，组合示例见第 15 节。

- 引用是受控标识，不是任意绝对路径；按 Thread/Workspace 归属校验读取。
- 单件最多 1 MiB/10000 记录，限制单 Turn 和总保留正文/manifest；这是逻辑配额，不是整个 Session/WAL 的物理磁盘上限。
- 完整输出与模型片段的摘要分别标记；部分写入不能标记完整。
- 过期返回 artifact_expired；默认禁止删除仍在执行中的引用。
- Artifact 不是通用 DLP。凭据文件默认不读，环境不透传秘密，日志不复制参数/原始异常；后续导出需单独授权。

## 8. 本地写与 Patch 准入

0.5.3a 的准备器仍只读；0.5.3b1 仅对工厂创建的私有副本开放宿主写 API，见第 17 节与 [ADR 0028](adr/0028-managed-patch-execution.md)。Kernel/模型写入已由 b2b 的专用端口开放，默认仍只读，见第 20 节。

Patch 不是简单字符串替换。实施前必须明确本地效果类型如何与现有 Action/Agent 契约兼容，不把本地修改伪装为 READ_ONLY，也不宣称所有 Patch 天然幂等。

- 解析补丁为计划，检查所有路径、完整前镜像/预期哈希和大小；不能以读取片段的摘要作为前置版本。
- 执行前持久化意图，记录受信计划指纹、目标与前/后内容证据；批准的是具体计划，不是“允许任意写”。
- 审批后与提交前再次检测漂移。工作区写串行只协调本 Agent，不能约束外部编辑器；不声称仅靠 hash+rename 就获得跨进程文件 CAS。
- 首先验证单文件原子替换及 fsync 边界；多文件 rename 不具有整体原子性，必须使用计划/日志、明确部分完成/恢复语义，不能伪报全有或全无。
- 已发生效果但 Result 未提交时进入可解释的未知/待核对状态；按前/后证据检查，不盲目重新应用补丁。
- 发现用户修改或第三种内容时停下并报告冲突；不 reset、checkout 或覆盖用户原改动来“恢复”。

## 9. Process、Git 与测试执行

- 使用 argv 和显式 cwd；没有隐式 shell=True。需要 Shell 语法的调用是单独的高权限能力，不通过“安全命令名”猜测整段命令安全。
- stdout/stderr 并发有界排水，达到展示上限后仍排水或明确终止，不能因缓冲填满造成死锁。
- 独立进程组，取消/超时先终止、等待宽限，再升级终止并 wait/reap；覆盖派生子进程、管道持有者与父进程提前退出。
- 环境采用允许列表，不继承模型 API Key/SSH 凭据；可执行文件来源与版本由宿主确认。
- run_tests 是实际代码执行，不因为名称叫“测试”就归类只读或默认免审批。Git 只读命令也必须禁用可能的外部 diff/pager/hook 路径。
- 没有 Sandbox 时只提供明确批准的宿主执行模式，不默认让不可信仓库代码运行；0.7 提供更强隔离，不倒推声称 0.5 已有。

## 10. 取消、错误与恢复

复用 CancelToken 与现有错误分类。预期业务错误转换为有界 ToolResult；内部 Schema/运行时不变量破坏不能伪装成正常结果。

初始错误包括 invalid_arguments、path_denied、not_found、wrong_file_type、invalid_utf8、limit_exceeded、workspace_changed、patch_conflict、process_timeout、output_unavailable；具体稳定代码在各切片契约测试中冻结。

取消 asyncio.to_thread 不会停止底层线程。不能提前发布 cancelled 后让后台写继续发生；读写实现必须定义可观察的回收边界，无法确认效果时使用 unknown，而不是编造回滚成功。

进程恢复只解释持久事实；未执行的调用与已发生效果但未提交结果必须可区分。Artifact 已写/ToolResult 未提交、Patch rename 前后、Shell 启动/退出记录前后均纳入崩溃矩阵。

## 11. 交付验收矩阵

- 输入：严格类型、额外字段、未知工具/版本、非法路径、参数重放与指纹改变。
- 文件：空文件、中文/跨块 UTF-8、非法编码、二进制、长行、大文件、链接、特殊文件、根/目录交换。
- 输出：精确限制边界、分页变更、确定性排序、Artifact 归属、过期、磁盘满与取消清理。
- 写入：脏工作区、前镜像漂移、重复计划、多文件部分失败、效果已生效但结果缺失。
- 进程：大量双流、非零退出、超时、Task/用户取消、孙进程、残留文件描述符、环境凭据隔离。
- 集成：实际 SDK 离线传输 → Kernel → ToolRuntime → 真实临时仓库 → SQLite → Replay；默认 CI 零 API 调用。
- 编码 Eval：固定受控缺陷、独立隐藏验收测试、实际 Git Diff、无意外文件修改、最终报告与真实命令结果一致。

每片运行 make check、异步调试及对应的跨平台/安装验证，文档同时更新。只有 0.5.5 的真实缺陷修复闭环通过，才可以宣称已具备该范围内的编码能力；不能用读取一个文件的成功代替完整 Coding Agent 验收。

## 12. 0.5.1 当前交付与使用

本片新增 `tools/contracts.py`、`workspace.py`、`files.py`、`runtime.py`，未修改 Kernel、Action Contract 或 Session Schema。固定受信绑定表只包含 `list_files` / `read_file`；输入/输出 Schema 已生成到 `spec/list-files-*-v1.schema.json` 与 `spec/read-file-*-v1.schema.json`。工具版本包含根身份、路径策略和执行契约摘要，模型不能提交或降低权限。

~~~python
from pathlib import Path
from harnessix.agent.runtime import AgentRuntime
from harnessix.tools.runtime import CodingToolRuntime

# store/provider 由宿主按已有接口构造；Session 应放根外或被拒绝的目录中。
async with CodingToolRuntime(
    Path("/由宿主确认的项目目录"),
    denied_paths=("private",),
    require_approval=True,
) as tools:
    async with AgentRuntime(store, provider, tools) as runtime:
        # 使用既有 create_thread/run_turn/reply_approval/resume_turn。
        ...
~~~

只读验收入口（不调用模型，不运行仓库代码）：

~~~bash
uv run python -m examples.kernel_files
uv run pytest tests/tools
PYTHONASYNCIODEBUG=1 uv run pytest tests/tools -W error
~~~

当前新增测试覆盖固定工具契约、真实文件与目录、严格输入、权限/链接/替换竞争、扫描/编码/分页、两个取消入口与 FD 回收、真实 SDK 离线闭环、审批重开及根/规则漂移、3 个真实进程退出边界。CLI 中尚无交互编码命令；该入口是可复查的只读验收，不是编码 Demo 冒充生产完成。

本地验收：新增 70 项通过；全量 `make check` **890 passed、1 skipped**，独立基础 wheel/无供应商 SDK 的只读入口通过。全项目硬崩溃切点从 54 增至 57，原 2 个 SIGINT 用例保留。异步调试和远端 CI 结果见 [测试验收记录](testing-and-evals.md#14-051-只读编码工具验收2026-09-03)。CI 新增 macOS 只读回归，Linux 完整套件保持；不把待运行的远端 CI 提前标记成功。

后续 0.5.2 已拆成 a/b，搜索交付见下节；Artifact 与写工具仍分别经过 0.5.2b/0.5.3 门禁。

## 13. 0.5.2a 当前交付与使用

`CodingToolRuntime` 固定广告四个只读工具，仍经原有 Kernel、持久审批、顺序执行与取消回收。新搜索预算/语义进入自身工具版本；旧 list/read v1 Schema 和版本未改写。实际 os.open 权限不足保留 PermissionError，用于报告不可读扫描缺口；原 read_file 同时得到更准确的 tool_path_denied，而非将权限不足误报为 workspace_changed。

~~~bash
uv run python -m examples.kernel_search
uv run pytest tests/tools
~~~

调用参数示例（模型参数，不是另一个执行 API）：

~~~json
{"tool":"glob","arguments":{"path":"src","pattern":"**/*.py","max_results":100}}
{"tool":"grep","arguments":{"path":"src","query":"ModelAttempt","include":"**/*.py","max_results":50}}
~~~

结果中的路径始终相对 Workspace；模式相对 path。grep 每行至多一条命中，包含 line、最多 384 UTF-8 字节 text、text_truncated 与 revision；用它构造 read_file 的 path/start_line/expected_revision，即可校验后续读取。按 LF 分行，去除末尾 LF/CRLF；未终止行的单独 CR 不改写。

| 边界 | 当前行为 |
| --- | --- |
| 查询与模式 | ≤256 UTF-8 字节；模式 ≤32 段；不支持任意正则、花括号或绝对路径 |
| 候选发现 | ≤10000 项、2 MiB 名称、32 级相对深度；先收集后全路径排序 |
| 内容读取 | 单文件 ≤2 MiB、合计 ≤16 MiB；严格 UTF-8/二进制检测，单行 ≤4 KiB |
| 返回 | ≤200 项、记录 ≤24 KiB、总 JSON ≤60000 字节 |
| 完整性 | 数量/输出截断返回 truncated/reason；不可读/超大/非法编码/二进制/长行计数，并设 scan_complete=false |
| 硬预算/变更 | 枚举或总读取超限、超时、观察到对象替换时失败，不把此前片段当作完整成功 |
| 忽略 | 固定性能目录可用 include_ignored 放宽；宿主拒绝路径、链接和敏感名称不能放宽；不解析 gitignore |
| 恢复 | 审批重开验证工具/工作区版本；中断后不自动重搜；搜索无分页游标或隐藏的完整输出 |

scan_complete 只针对本次定义的可搜索范围，不是全树原子快照。计数说明观察到的缺口，并非对截断后未读范围的统计；空命中且 scan_complete=false 不能解释为不存在。字符片段裁剪与结果截断是两件事：前者仍可能扫描完整，后者一定不完整。

新增四份 `glob/grep-input/output-v1.schema.json`，不修改 Agent v5、Action v1 或旧工具 Schema。搜索示例使用固定离线 Provider 消费真实文件输出，不是自主编码 Eval；默认测试不使用真实模型或服务器。验收记录见 [测试文档](testing-and-evals.md#15-052a-有界搜索验收2026-09-03)。

0.5.2b 拆为 b1/b2，上下文和 Artifact 分别见以下两节；两片现均已实现，0.5.2 范围闭环。

## 14. 0.5.2b1 当前交付与使用

新增 `agent/execution.py` 的 `ToolExecutionScope` 和 `agent/ports.py` 的 `ScopedToolRuntime`，不新建 Registry、Agent Loop 或 ContextVar。两个宿主入口互斥：

~~~python
# 旧接入保持原样：execute(call, cancel)
async with AgentRuntime(store, provider, tools) as runtime:
    ...

# 显式选择新接入：execute_scoped(call, scope, cancel)
async with CodingToolRuntime(root) as tools:
    async with AgentRuntime(store, provider, scoped_tools=tools) as runtime:
        thread = await runtime.create_thread(str(tools.workspace_root))
        turn = await runtime.run_turn(thread.thread_id, "搜索目标函数", request_id="search-1")
~~~

Kernel 在只读/版本/审批门禁之后，从最新持久投影核对活跃 Turn 和未完成调用，再注入不可变上下文。作用域包含真实 Thread/Turn/Call、宿主 Workspace 标签及复用既有审批算法的完整调用摘要。模型参数不能覆盖这些字段；通用工具仍负责自身参数校验，CodingToolRuntime 的严格模型拒绝额外归属字段。

CodingToolRuntime 的新入口还要求 workspace 严格匹配其规范根，随后复用原有根身份、策略、输入/输出、取消和 FD 检查。宿主应使用 workspace_root 创建 Thread；不匹配报 tool_workspace_mismatch，不通过别名重新 resolve 悄悄修正。0.5.2b1 当时保持旧定义、八份 list/read/glob/grep Schema、Agent v5、审批指纹与 Migration 不变；后续 b2 新增 migration 6，默认工具定义仍不变。

`examples.kernel_search` 已切换到显式 Scoped 入口，`examples.kernel_files` 保留旧入口，独立 wheel 和跨平台 CI 同时验证二者。作用域不自动给用户开放 Artifact、文件写入或 Shell，也不是在 Turn 结束后仍有效的发布租约。详细边界见 [ADR 0025](adr/0025-trusted-tool-execution-scope.md)，验收见 [测试记录](testing-and-evals.md#16-052b1-可信执行作用域验收2026-09-03)。

0.5.2b2 已选择同一 Session 事务，避免外部 blob 的双写与孤儿协议，交付见下节。

## 15. 0.5.2b2 当前交付与使用

本片新增 `artifacts/contracts.py`、`ports.py`、`sqlite.py` 和 migration `0006_artifacts.sql`，以及六份独立 Artifact/归档输出 v1 Schema。该片当时事件和投影仍为 Agent v5，旧 migration 与八份默认只读 Schema 原样保留；**数据库 migration 6 与 Agent Schema 5 不是同一个版本号**。旧程序拒绝新库；不是可降级升级。

~~~python
from pathlib import Path
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime

# provider 沿用现有 Provider 端口；Session 放在工作区外或被拒绝的私有目录。
session = SQLiteSessionStore(Path("/宿主私有目录/session.db"))
artifacts = SQLiteArtifactStore(session)
async with CodingToolRuntime(root, artifacts=artifacts) as tools:
    async with AgentRuntime(session, provider, scoped_tools=tools, artifacts=artifacts) as runtime:
        thread = await runtime.create_thread(str(tools.workspace_root))
        turn = await runtime.run_turn(
            thread.thread_id, "搜索并读取更多命中", request_id="archive-1"
        )
~~~

启用后搜索输出变为 `{preview, artifact}`；preview 仍受原有行/字节限制，artifact 为 `ArtifactRef`，无内部数据库路径/Thread 身份字段。`glob` JSONL 每行是路径字符串，`grep` 每行是含路径、行号、片段和 revision 的对象。`read_artifact(artifact_id, offset=0, limit=100)` 使用 **0 起始记录偏移**，单页最多 200 条/24 KiB UTF-8，返回 `text` 与 `next_offset`；结束为 null。原始片段里的转义换行不被误当作记录边界。

| 边界 | 当前行为 |
| --- | --- |
| 完整性 | `preview.truncated=true` 可与 `artifact.complete=true` 同时成立；后者指定义范围扫描完成，不是无限日志、全仓快照或完整源码行 |
| 捕获预算 | 单件最多 1 MiB/10000 记录；任一硬上限触发即失败，不保存伪完整前缀；扫描/权限预算不放宽 |
| 默认配额 | 单 Turn 累计 4 MiB/128 件；保留正文 32 MiB；累计 10000 份 manifest（含 tombstone） |
| TTL | 默认 24 小时，可配置 60 秒至 7 天；过期读取明确失败，读取不续期 |
| 归属 | 真正的 Thread 与 Workspace capability 由宿主提供；跨 Thread 或重开根/拒绝策略变化不返回内容 |
| 事务 | 内容、manifest 与 ToolResult 的事件/投影共用一个 SQLite 事务；回滚没有孤儿，提交后有成对事实 |
| 清理 | 宿主显式 `await artifacts.collect(limit=100, after=cursor)`；返回 `next_after`，全批可能需再读空批确认结束；下一轮从无 cursor 开始 |
| 活跃引用 | 活跃 Turn 的整个 Thread 保守保护，跳过正文清理但不延长读取有效期 |
| 失败 | not_found / expired / corrupt / invalid_cursor / quota_exceeded 等明确区分；缺失与跨归属统一为 artifact_not_found |
| 恢复 | 进程中断不重新搜索；Replay 可恢复历史引用，不重新生成缺失/过期正文 |

清理只释放正文并保留 tombstone，不立即缩小数据库文件；配额不覆盖历史事件、SQLite 空闲页或 WAL 的物理大小。达到 manifest 累计上限后需要宿主轮换/保留管理，不自动删除用户会话。进程标准输出、任意二进制/流式 blob 和导出尚不在本片支持范围。

`uv run python -m examples.kernel_artifacts` 在临时真实文件中搜索 300 条中文命中，模型预览只有 2 条，随后读取第 299/300 条，重开 SQLite 并核对 Replay；固定离线决策不等于自主编码 Eval。质量证据见 [第 17 节验收](testing-and-evals.md#17-052b2-事务-artifact-验收2026-09-03)。

**0.5.3 演进**：已完成只读计划及写准入 ADR 基线，复用既有 Call/Result 与效果分类，明确完整前镜像、目标计划审批、持久写意图、文件系统与 Session 非原子的核对边界。首先交付单文件 Patch，逐一验证脏工作区、编辑器竞争、提交前后崩溃及第三种内容冲突；多文件效果和 Process 必须分别验收，不能以简单替换或删除 READ_ONLY 门禁代替。

## 16. 0.5.3a 当前交付：只读准备与复核

新增 `patches/contracts.py`、`planner.py`，提供宿主同步 API `prepare_patch(workspace, proposal, operation)` 和 `verify_prepared(workspace, prepared, operation)`。新增两份独立 v1 Schema，不改变 Kernel、旧工具定义、Action/Agent Schema 或 migration 6。该准备器不向模型广告 Patch 工具，不自行持久化或执行；受管执行见第 17 节。

~~~python
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.planner import prepare_patch, verify_prepared
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace

with Workspace(root) as workspace:
    page = read_file(workspace, ReadFileInput(path="main.py"), ReadOperation())
    proposal = PatchProposal(
        path="main.py",
        expected_revision=page.revision,
        edits=(ExactEdit(old_text="return a - b", new_text="return a + b"),),
    )
    prepared = prepare_patch(workspace, proposal, ReadOperation())
    verify_prepared(workspace, prepared, ReadOperation())
    # 仅得到 before/after 私有字节及 manifest，不会修改 main.py。
~~~

限制：单文件前后内容各最多 1 MiB，1–32 个非空唯一锚点，单编辑 UTF-8 合计 128 KiB，全部编辑 256 KiB。不支持创建/删除/移动、空文件插入、统一 Diff 解析、模糊替换或全局替换。可以将一个非空精确锚点替换为空字符串；未涉及的字节保持不变。多个编辑定位在同一前镜像，输入顺序不改变目标内容；指纹仍绑定具体提案顺序。

完整前镜像包含预览外内容；SHA 是全文件内容 SHA，不等于 read_file 的状态 revision。manifest 为宿主契约，含 Workspace 摘要，不作为模型参数或授权凭证；before/after 不出现在 PreparedPatch 的 repr，私有内容仍需宿主按保留/保密规则管理。JSON manifest 可重开校验，但没有正文就不能恢复整个 PreparedPatch。

复核会再次读取并比较完整源文件、revision 与权限位。它不能锁住未来写入前的间隙，更不是跨进程 CAS。线程中运行时由宿主使用 ReadOperation 协作停止并等待线程退出后再关闭 Workspace；本片没有增加另一套异步调度器或声称能中止不可中断的内核 I/O。

`examples.patch_plan` 验证真实 CRLF 文件的目标内容计算、源文件保持不变，以及模拟外部编辑后的复核拒绝。它不执行 Patch，也不属于自主编码 Eval。上述宿主写前置条件现由 b1 受管后端实现；模型接入现见 b2b（第 20 节）。源目录并发编辑的无覆盖保证不能由 hash+rename 推导。


## 17. 0.5.3b1 当前交付：受管单文件执行

设计决策见 [ADR 0028](adr/0028-managed-patch-execution.md)。新增模块只依赖现有契约和 Python 标准库，未修改 Agent v5、Action v1、Session migration 6、默认工具定义或 Kernel READ_ONLY 门禁。

### 宿主 API 与生命周期

| API | 含义 |
| --- | --- |
| `PatchWorkspaces(private_root)` | 管理根为当前 UID 的 0700 私有目录，必须位于源工作区之外 |
| `create(source, paths, operation)` | 明确选择文件，建立 UUID 副本；逐文件快照，不是完整 Git worktree |
| `open(workspace_id)` | 核对目录/锁/数据库/工作区身份，取得单宿主锁；building 副本拒绝重开 |
| `save(prepared, request_id, operation)` | 在副本上准备后保存完整计划；同请求同载荷幂等，不同载荷冲突 |
| `reply(plan_id, approval_fingerprint, decision)` | 批准/拒绝只记账；指纹绑定副本/计划/请求/内容，答复重试不执行 |
| `execute(plan_id, approval_fingerprint, operation)` | 只接受 approved；先消费写意图，再写临时文件和替换，最多执行一次 |
| `get(plan_id)` | 读取校验后的持久状态，不重新执行 |
| `reconcile(plan_id, operation)` | 对 started/uncertain 作只读效果观察；不生成重新执行许可 |
| `close()` / 上下文管理器 | 等待当前同步操作结束，关闭数据库/锁/FD；不删除副本 |

`create` 返回的 `copy.workspace` 可交给已有只读工具和 `prepare_patch`。计划必须基于**副本**的 revision，而不是源文件 revision。宿主不得把该副本交给不受管的并发编辑器/进程。API 是同步且协作取消的；在线程中调用时必须等待后台操作清理完成。b1 不提供 asyncio Task 的取消封装，b2 接入时另行验证。

### 状态与不确定性

~~~text
pending → rejected
pending → approved → started → applied
                         ├──→ failed      （替换尚未尝试）
                         └──→ uncertain   （替换已尝试，但结果未获充分确认）
started / uncertain → observed_before / observed_after / diverged / missing / unavailable
~~~

`started` 的第二条事件可补充持久临时 inode。后镜像字节/权限和临时 dev/inode 全部吻合才形成 observed_after；仅后镜像字节相同仍为 uncertain。已记录的终态观察不会自动刷新为新事实，get 返回的是历史观察；若仍 uncertain，可再次显式核对。根/账本/锁身份失效直接拒绝操作，不在不可信账本里补写 unavailable。父目录/目标不可读取可记录 unavailable。即使 observed_before，要重新尝试也必须新 request_id、新计划和新批准。

替换之后收到协作停止信号时先完成效果落盘与记账，不假报回滚。文件已变而结果账本写失败时保留 started/uncertain，宿主重开核对，不重复应用。`os._exit` 验证进程崩溃，不代表断电/存储控制器故障测试。

### 文件、配额与保留

- 布局：`<private_root>/<UUID>/workspace/` 为可读副本；同级 `owner.lock`、`ledger.sqlite` 与 `<plan_id>.patch` 不暴露给模型。
- 只导入普通单链接 UTF-8 文件；最多 256 个、单个 1 MiB、合计 32 MiB。来源仅存摘要/revision 和私有基线字节，不依赖源目录后续存活。
- 只复制内容/普通权限位，不复制源 ACL/xattr/所有权/Git 元数据。目标拒绝特殊权限、非当前 UID、用户扩展属性和扩展 ACL；Darwin 允许私有新文件上的 `com.apple.provenance` 系统标记重新生成，不声称保留它的值。检查不可用时拒绝写，不静默跳过。
- 每副本最多 64 个计划，前后镜像合计 32 MiB；来源基线另受 32 MiB 限制。不是整个 SQLite/journal 或文件系统物理大小上限。
- SQLite 使用独立 application_id、schema v1 和同步事务，追加状态事件校验归属/迁移/载荷。不是对同 UID 攻击者防篡改的日志，也不新增租户/远程认证。
- 正常失败只清理本次已知 inode 的临时文件；硬崩溃可能留有私有临时文件或 building 副本。当前不自动回收整个副本；宿主在关闭所有持有者并确认交付/保留要求后管理其目录，不自动删除未知内容。
- b1 不创建/删除/移动目标文件，不多文件提交、不运行源码、不导出/自动合并到源目录。

运行 `uv run python -m examples.managed_patch` 可验证完整宿主链路。它是执行后端的可运行验收，不是自主编码能力或 0.5.5 Eval。

### 0.5.3b2 的实施顺序（已交付）

实施进展：已先交付 **b2a 宿主桥接**（第 18 节），完成稳定调用绑定及异步收尾前置条件；以下完整 Agent 事件/审批/模型接入归入 **b2b**，已独立完成第 20 节的组合验收，不以宿主桥接验收抵扣。

1. 固化 Agent 写审批/结果契约与兼容迁移；不改义 kernel-read-only/v1。
2. 在受信 Scoped 入口绑定 Thread/Turn/Call、受管副本和持久 plan_id；模型参数不得注入授权或归属。
3. 明确模型提交提案、生成宿主计划、等待审批、消费计划、发布结果的顺序；旧只读审批仍按旧规则运行。
4. 以稳定请求 ID 连接 Session 与副本账本；覆盖“计划已保存/Session 未提交”“文件已写/ToolResult 未提交”等非原子窗口。
5. 接入最小模型 Patch 工具；专用非幂等写准入，不放开任意 NON_IDEMPOTENT_WRITE 工具。
6. Kernel 取消/超时必须回收后台写线程；重启只加载/核对已有计划，不把历史 Call 再执行。
7. 增加真实 SDK 离线读→提案→审批重开→写→读回→回答集成、跨版本旧审批，以及 Kernel × 文件替换真实崩溃矩阵。
8. 通过全量回归、独立 wheel 与 Linux/macOS CI 后，才关闭 0.5.3b；真实 API 仍需独立预算授权。

## 18. 0.5.3b2a 当前交付：宿主调用绑定桥接

设计见 [ADR 0029](adr/0029-managed-patch-agent-bridge.md)。新增 `patches/agent_bridge.py`、`bridge_contracts.py`，复用既有只读调用归属、提案准备器和 b1 受管后端。只新增两个独立 v1 Schema，**该片当时 Agent v5 / Action v1 / Session migration 6 / 副本账本 schema v1 和默认工具清单不变**。`ManagedPatchBridge.definition()` 返回单一写定义，不是通用 ToolRuntime；通用注册表仍拒绝该非只读调用，b2b 使用专用端口接入。

### 宿主 API

| API | 本片语义 |
| --- | --- |
| `ManagedPatchBridge(copy)` | 绑定宿主已取得所有权的一个受管副本，不接受任意可写目录 |
| `definition()` | 固定 apply_patch 提案契约，non_idempotent_write、高风险、必须审批、可核对；不自动注册 |
| `prepare(call, scope, cancel)` | 验证调用/副本/严格提案；按稳定 request_id 查找原计划，仅缺失时准备并保存；返回 ManagedPatchCallPlan |
| `review(call, scope, plan, cancel, verify_source=True)` | 仅 pending 可复核；验证保存计划，默认复核完整前镜像；b2b 拒绝路径可显式跳过来源复核，不记录决定、不写文件 |
| `execute(call, scope, plan, approval, cancel)` | 验证桥接审批指纹，镜像宿主决定到后端；批准走一次性执行，拒绝不改文件；已消费计划不重试 |
| `recover(call, scope, cancel, plan=None, approval=None)` | 只查找/读取/reconcile；不 prepare/save/reply/execute；可找回保存后尚未发布给 Session 的计划 |
| `aclose()` / `async with` | 排空本桥接的后台操作，拒绝后续操作；不关闭或删除宿主副本 |

后端新增 `lookup(request_id, operation)` 和 `verify(plan_id, operation)`，分别只加载既有计划及复核完整前镜像；不修改旧 get/save/execute 契约，不迁移已有副本数据库。

### 调用与私有证据

`ManagedPatchCallPlan` 包含 Thread/Turn/Call、调用摘要、稳定请求、副本/计划身份、完整 manifest、后端指纹与桥接审批指纹。最后者绑定整份计划，不等于后端指纹或 kernel-read-only/v1 的执行摘要。模型参数仅是 PatchProposal：相对路径、expected_revision、精确 edits；注入 actor、plan_id、scope、批准标志等全部拒绝。

`PatchCallResult.result` 是现有 ToolResultContent；output 仅含版本、相对路径、历史状态、前后内容 SHA。`plan` / `record` 单独留给宿主，不进入模型结果。原提案仍可能含用户代码，副本私有账本仍持有前后镜像；本片不声称代码从未进入模型/日志，宿主需保留既有数据处理边界。

### 恢复、取消与授权边界

- 缺少计划且调用方也未提供持久计划或审批时报告未成功；提供了 plan 或 approval 任一证据而磁盘找不到时为 unknown，不假定未执行。
- pending/approved 没有消费写意图，rejected/failed/observed_before 已知未成功；恢复不会据此自动重试。
- started/uncertain 先做归因观察。applied/observed_after 还须匹配宿主批准才能报告 succeeded；缺批准、错绑定、第三种内容、缺失、不可读、账本异常为 unknown。
- 调用契约、参数或执行作用域本身无效时直接抛出结构化错误；上条 unknown 指进入账本核对后发现的不一致，不能把入口异常自动解释为未产生效果。
- 已应用状态是历史事实；不能据此断言文件此刻未被外部编辑。执行抛错不等价于无效果，调用方必须核对，不能统一转成失败。
- 桥接使用串行线程和 ReadOperation；协作取消、Task.cancel、外层超时或重复取消必须等待写线程退出。替换前停止可未应用；替换后先完成效果与记账。close 同样等待；没有不可中断 I/O 的硬实时终止保证。
- **桥接层不承担 Session 授权核验**：ApprovalRecord 是宿主声明，本层不验证当前 Turn、截止时间或是否已消费审批。完整 Kernel 接入必须先持久审批并消费恢复边界，再调用 execute；不能把旧 READ_ONLY 审批当作写授权。

`uv run python -m examples.patch_bridge` 串联真实只读工具→精确提案→持久计划找回→宿主批准→写入→读回→重开核对。它证明桥接与既有组件互通，不是模型驱动的编码 Eval。b2b 已复用这一桥接，不另写文件替换器或恢复执行器。

## 19. 0.5.3b2b 设计审查记录（实施前）

已核对 Runtime、Reducer、Scope、Session 与模型消息白名单，形成 [ADR 0030](adr/0030-kernel-managed-patch-admission.md)。设计覆盖独立写审批、拟定 Agent v6/migration 7、显式 Patch 端口、审批答复与消费时序、工具效果和 Turn 状态分开结算，以及 KWP-01 至 KWP-10 验收矩阵。

设计审查时补齐现有桥接的一个恢复分支：只传 ApprovalRecord、未传 plan 且账本缺失时必须 unknown，不能误报为已知失败。新增无证据/批准/拒绝三项回归，先复现后修正；不改变公开 Schema、既有 migration、默认工具或 Kernel。

设计提交 `45b2b10` 当时仍为 Agent v5/migration 6；实际实现和验收见下节，不追溯修改该设计提交的能力声明。

## 20. 0.5.3b2b 当前交付：Kernel 受管写闭环

### 接入与所有权

`AgentRuntime(..., patches=bridge)` 接受专用 `PatchRuntime`，当前实现为既有 `ManagedPatchBridge(copy)`。同一受管副本根交给 `CodingToolRuntime` 和 `create_thread`；必须使用副本的 read_file revision。写定义必须为 apply_patch、NON_IDEMPOTENT_WRITE、强制审批/幂等绑定且支持核对；重名拒绝。不配置 patches 时与旧行为一致，通用注册表的写定义不能绕过门禁。

宿主进入顺序为受管副本→桥接/读取工具→Kernel，逆序退出。Kernel 关闭先排空活动 Turn 和审批复核，重复取消不提前释放 Session 所有权；桥接排空线程，最后释放副本。这里没有增加 OS Sandbox、源目录写入或模型身份认证。

完整可执行宿主组合见 `examples/kernel_patch.py`：

~~~bash
uv run python -m examples.kernel_patch
~~~

它使用真实文件、真实 Session/副本账本和离线 Provider，读取旧 revision 后提案，停在 WAITING_APPROVAL，关闭再打开原副本与 Session，宿主批准后显式 resume，最后读取新内容和 Replay。模型不构造计划 ID、审批决定或作用域。

### 新持久契约与顺序

- Agent Event/Thread **v6**、Session **migration 7**；Action v1、副本账本 v1、既有工具/桥接 Schema 不变。
- 新 `PatchApprovalRequestContent` 的 kind/策略与旧只读审批分离，保存完整不可变调用计划；只读审批原语义不变。
- `ToolResultContent.patch` 是固定有界的私有效果证据，包含副本/计划/请求/审批指纹、状态和 execution/recovery 来源；不含完整镜像。无 patch 时序列化不增加 null 字段，旧事件原文和导出形状保持兼容。
- Session Call→副本计划→Session 审批等待→Session 决定→Session 消费等待边界→后端镜像决定/一次性执行→Session 结果。答复不执行；重复答复相同内容幂等，冲突拒绝；拒绝不要求旧来源仍存在，但归属/计划必须有效。
- 复核后再次检查原始截止时间，决定时间在复核后生成。审批等待、关闭重开和 resume 均不刷新预算。

### 结果与恢复

已应用事实与 Turn 结果分开：替换后取消可以是工具 succeeded、Turn cancelled；进程重启核对出成功仍为 interrupted，不自动完成 Turn 或继续模型。来源漂移、错误工作区、失去原桥接、契约变化或无法充分归因都不能自动退回通用写入/新计划。

恢复只查找/核对，已知未应用也不重新 execute；需要再尝试时必须新的调用/计划/审批。公开结果受模型正文预算限制，私有证据不占公开预算。写入后结果超限终止 Turn，核对并保存效果；必要时丢弃公开 output，绝不丢弃绑定字段或重新写。

测试覆盖两个实际 SDK 的离线 HTTP 全链路、审批/绑定/严格参数、替换前后四类取消、关闭/迟到答复、输出预算、旧 wheel 升级以及 Session × Patch 的真实进程退出。详见 [验收第 22 节](testing-and-evals.md#22-053b2b-kernel-受管写闭环验收2026-09-04)。b2/b 的受管单文件范围已交付；多文件部分效果和结构化 Diff 现按 ADR 0031 分为 c1/c2/c3；c1 交付见下节，不承诺跨文件原子或自动回滚。

## 21. 0.5.3c1 当前交付：只读整组计划与结构化 Diff

设计见 [ADR 0031](adr/0031-patch-batches-and-structured-diff.md)。新增 `batch_contracts/batches/diff_contracts/diff`，只将原准备器的精确区间解析提取为共享函数；没有另建替换器、写账本或审批入口。

### 宿主 API

| API | 当前语义 |
| --- | --- |
| `PatchBatchProposal(files=(...))` | 复用单文件 PatchProposal，有序且路径唯一，不自动重排/合并 |
| `prepare_patch_batch(workspace, proposal, operation)` | 先验证整组，逐文件只读准备，累计完整镜像预算，最后复核所有来源；失败不返回半组计划 |
| `validate_patch_batch(workspace, batch, operation)` | 只核对内部载荷/提案/manifest/共同工作区，不读取当前文件 |
| `verify_patch_batch(workspace, batch, operation)` | 内部核对后逐项读取当前来源；不是跨文件同时刻快照或提交 CAS |
| `patch_batch_diff(workspace, batch, operation, options=None)` | 基于已验证计划生成结构化编辑预览，不观察当前工作区或证明已写入 |
| `PatchDiffOptions(max_output_bytes=65536, preview_bytes=1024)` | 宿主显式预算；序列化 JSON 总量256字节–1 MiB，片段0–4096 UTF-8字节 |

以上同步 API 使用同一个协作取消/截止时间；在线程中调用时宿主必须等工作线程退出，不因取消外层等待就释放 Workspace。

### 计划与 Diff 契约

最多16文件；编辑旧/新文本合计512 KiB；完整前后镜像合计8 MiB，原单文件1 MiB限制保留。整组 manifest 绑定有序单文件 manifest、提案与工作区；重排改变整组指纹，私有正文不进入 repr。准备较晚文件期间较早文件漂移会被最终复核拒绝，仍不承诺全组原子快照。

Diff 的每项包含路径、文件计划指纹、按原文偏移排序的序号、前/后字节位置以及片段长度、完整 SHA、文本前缀/截断标记。后坐标累计此前编辑长度差，不是字符或行坐标；删除的后片段可为空。完整报告配合计划前镜像可按坐标重建目标内容；预览截断时不能据此重建完整结果。

报告 total_files/total_edits 总是整组数量，edits 是预算内前缀，truncated 同时覆盖未返回编辑与文本前缀。按真实 UTF-8 JSON 序列化长度限额，包含引号、反斜杠、制表符等转义成本。预算不足容纳首项时允许空 edits，但保留总量/指纹且明确 truncated。这不是统一补丁或完整 Git Diff。

### 验收与后续

`uv run python -m examples.patch_batch` 在两个真实文件上验证 BOM/CRLF 保留、坐标重建、256字节截断、重开整体复核且磁盘无修改。四份独立 v1 Schema 覆盖整组提案/manifest、Diff 和预算；旧 Schema、Agent v6/Session migration 7/副本账本 v1/模型工具清单保持不变。

本片没有组审批、组效果状态、组账本、自动 Artifact 发布或新模型工具。下一片 c2 先固化实际账本事务/成员预留和逐文件部分效果；c3 再升级 Kernel 并对接模型/Artifact。整组计划或截断 Diff 不能拿来绕过旧单文件审批，不能把当前准备器称作已完成多文件写入。具体测试记录见 [第23节](testing-and-evals.md#23-053c1-只读整组计划与结构化-diff-验收2026-09-04)。

## 22. 0.5.3c2a 当前交付：整组事务预留与持久审批

设计见 [ADR 0032](adr/0032-durable-batch-reservation-and-approval.md)。使用 `ManagedPatchBatches(copy)` 借用现有受管副本、互斥锁和生命周期，宿主需在 copy 关闭前使用。c2a 当时没有组 execute/reconcile；c2b 的显式宿主入口见下一节。模型工具仍不广告批量写，单文件 Kernel 路径保持原有行为。

| 宿主入口 | 行为 |
| --- | --- |
| `save(batch, request_id, operation)` | 验证完整私有计划；同请求同内容返回原记录，新组整组复核后在单事务预留所有成员 |
| `get(batch_id, operation)` | 只读加载完整绑定、成员镜像/事件和审批决定，缺失报错 |
| `lookup(request_id, operation)` | 只查已有组，缺失返回 None，不准备或创建 |
| `verify(batch_id, operation)` | 只读复核持久计划及当前所有前镜像，不更新状态 |
| `reply(batch_id, approval_fingerprint, decision, operation)` | 验证组指纹，持久保存唯一决定；相同决定幂等，不同决定冲突 |

返回 `ManagedPatchBatchApproval`：plan 为不可变完整 `ManagedPatchBatchPlan`，decision 为 None/批准/拒绝。批准答复不执行、不镜像成员批准，尚未显式执行时所有成员仍 pending。旧单文件 save 的幂等命中、reply、execute 拒绝组成员；归属列被清空时还会检查完整组计划，不能据此重新开启单文件写入。

组计划最多64 KiB UTF-8 JSON，元数据逻辑预留合计1 MiB，每组按计划实际字节加16 KiB决定空间计算。成员占用原64计划/32 MiB前后镜像配额，检查和插入均在同一事务。批准后的文件漂移不改写原批准；后续执行必须重新复核，当前 verify 会拒绝陈旧前镜像。超时/取消可能发生在提交确认之前或之后，调用方应 lookup 已有请求，不因返回异常就断言没有持久记录。

副本账本升级为 v2，Agent v6/Session migration 7/Provider v3/旧单文件 Schema 不变。新旧 wheel 升级证据与11个真实提交/迁移退出切点见 [测试第24节](testing-and-evals.md#24-053c2a-整组预留持久审批及迁移验收2026-09-04)。后续 c2b 已按 ADR 0033 实现顺序消费、部分/未知效果和只核对恢复；组事务预留仍不承诺文件修改的组原子性。

## 23. 0.5.3c2b 当前交付：顺序消费、部分效果与只核对恢复

见 [ADR 0033](adr/0033-batch-consumption-and-effect-recovery.md)。新增独立运行/效果契约、组运行事件表与 v2→v3 迁移；不改变原组计划/审批 Schema，不改变 Kernel 模型工具定义。原单文件 execute/reconcile 的内部核心只作提取；审查时与 `f0adddc` 的归一化 AST 对比完全一致，公开入口仍拒绝组成员。

| API | 含义 |
| --- | --- |
| `execute(batch_id, fingerprint, operation)` | 只消费已批准且未开始的组；返回终止原因与已知效果 |
| `get_execution(batch_id, operation)` | 只读运行与成员事实；未消费返回 None，组缺失仍报错 |
| `reconcile(batch_id, operation)` | 只观察已开始/未知成员，不重新执行；未消费不创建运行记录 |

BatchRunRecord 只保存完整组审批指纹/副本/组身份，以及 started/finished 和终止原因；每组最多开始、终止两个事件。BatchExecutionResult 从已校验的单文件事件组合有序成员状态和 not_applied/applied/partial/unknown，不在组表重复缓存可能过时的成员效果快照。每事件最大1 KiB，64计划上限下运行载荷最多128 KiB，非真实磁盘预分配。

执行顺序是：完整批准与未消费检查→持久组开始→整组前镜像/写准入复核→每成员组顺序检查→仅该成员镜像批准→原单文件意图/临时 inode 证据/替换/归因→组终止。整组复核失败也会消费批准，成员仍 pending；中途来源漂移可停在该成员 approved 而未进入文件意图，不将其记成执行失败。再次尝试须新请求/计划/审批。

| 情况 | 文件效果与终止原因 |
| --- | --- |
| 全部执行成功 | applied + completed |
| 最后一次替换后取消/超时 | 可以是 applied + cancelled/timeout，不抹去写入事实 |
| 成功前缀之后失败 | partial 或 unknown + failed，后缀 pending |
| 文件已改完、组终态未提交时崩溃 | 查询仍 started；只核对后 applied + interrupted |
| 后镜像字节相同但 inode 无法归因 | unknown，不补写、不按字节推断成功 |
| 存储异常导致结果不可发布 | 调用可能失败；通过已有开始/成员记录只核对，不能盲目重试 execute |

恢复中断后可再次只核对。仍 started 的组结束为 interrupted；已有 finished 保留原原因，未知成员观察可更新已知效果，但不追加新的组终止原因。结果是历史归因而不是实时文件完整性证明；已应用文件以后被外部修改，不会凭空抹去曾发生的效果。源目录、目标文件 inode/mtime/ctime 的恢复不写入证据及真实旧 wheel 升级见 [测试第25节](testing-and-evals.md#25-053c2b-顺序执行与部分效果恢复验收2026-09-04)。


## 24. 0.5.3c3a 当前交付：完整整组调用与宿主异步桥接

`ManagedPatchBatchBridge(copy)` 为宿主 API。它不实现通用 ToolRuntime，也不能传给 Kernel 的旧 `patches` 单文件端口。独立 `apply_patch_batch` 定义只供宿主检查，当前 Kernel 不向模型广告或执行它。

| 异步方法 | 实际语义 |
| --- | --- |
| `prepare(call, scope, cancel)` | 按完整调用稳定请求查找或准备/预留整组；已有计划不重建，所有来源复核 |
| `review(call, scope, plan, cancel, verify_source=True)` | 比较全部绑定且要求尚未决定；拒绝时可不检查当前来源，不记录批准 |
| `execute(call, scope, plan, approval, cancel)` | 宿主先持久消费等待；镜像整组决定，批准则调用既有一次性执行器，拒绝不创建运行 |
| `recover(call, scope, cancel, plan=..., approval=...)` | 只加载/核对原整组；缺完整宿主批准、事实丢失或错绑返回 unknown，不补批或重放 |
| `aclose()` | 拒绝新任务、排空活动线程，不关闭宿主副本 |

`ManagedPatchBatchCallPlan` 嵌入完整后端组计划，外层绑定 Thread/Turn/Call、完整调用指纹和自身审批指纹。独立请求标签避免与单文件或只读批准混用。模型不能提供作用域、计划 ID 或批准人。计划上限65 KiB（后端64 KiB加绑定1 KiB）；公开输出上限48 KiB，包含有序路径、前后摘要、成员状态/效果和运行阶段/原因，不包含原文、成员 ID、宿主身份或私有根目录。

`BatchCallResult.result` 为公开 ToolResult，其余 plan/approval/execution 是宿主私有事实，不进入模型 wire 或 repr。partial 返回 failed，含未知返回 unknown；全部已应用可以 succeeded，同时仍带 cancelled/timeout/interrupted 后端终止原因。结果不能覆盖 Turn 的终止原因；c3b 已另行持久化私有效果，见下一节。

每次方法的5秒读操作预算在等待锁前建立，不在入锁后刷新。**这不是 Turn 截止时间**；c3b 已从原持久 Turn 计算剩余时间，外层取消/超时排空桥接线程后结算。准备过程中取消可留下已持久的 pending 整组；批准镜像后、组开始前取消可留下批准但尚未运行的组。恢复只读事实，不据此授予重试许可。宿主未持久消费 Session 等待的场景不属于本片提供的执行授权。

c3a 当时保持 Agent v6/Session migration7/Provider v3/副本v3不变；当前 c3b 的 Session 升级见下一节，旧 Schema 与单文件后端不改。运行 `uv run python -m examples.batch_patch_bridge` 可验证真实两文件修改与重开核对。设计及 c3b/c3c 门禁见 [ADR 0034](adr/0034-batch-call-bridge-and-kernel-integration.md)，本片不宣称 c3 或 0.5 完成。

## 25. 0.5.3c3b 当前交付：Kernel 整组持久审批与恢复

### 接入和所有权

宿主显式注入 `patch_batches=ManagedPatchBatchBridge(copy)`，复用同一 AgentRuntime、模型循环与 Session。`patches` 单文件端口不变，二者可共存，不能彼此替代或重复注册；默认模型仍只有只读工具。先关闭 Runtime，再关闭桥接/只读工具，最后关闭副本；等待写入或审批复核的线程排空后才释放 Session 所有权。

`examples/kernel_batch.py` 提供两文件读取、整组提案、关闭/重开审批、真实修改、读回与 Replay。实际 SDK 集成由 MockTransport 驱动，每个供应商6次离线 HTTP 交互，不代表真实模型的自主编码能力。

### 持久事实与准入顺序

Agent v7 增加 `PatchBatchApprovalRequestContent`，完整嵌入 c3a 调用计划，独立策略 `kernel-managed-patch-batch/v1`。`PatchBatchRuntime` 的 prepare/review/execute/recover 复用原桥接；Kernel 在执行前验证真实活跃 Thread/Turn、首个待结算调用、原工具定义、完整计划/批准及原截止时间。

1. 保存模型调用，再向副本准备/预留完整组计划；
2. 同一 Session 事务记录整组请求与 WAITING；
3. 宿主 review 后仅保存 Session 决定，成员仍 pending，后端无决定；
4. 持久离开 WAITING，再镜像后端决定并执行一次性组运行；
5. 保存公开结果与私有 `patch_batch`，成功才继续模型。

两库不原子；Session migration8 只是最低 reader 标记，保留旧事件/投影原字节，副本账本v3不迁移。旧 v1–v6 Schema 保持冻结，新批量 Item/效果不得使用旧版本标签。

### 效果、失败与时限

`patch_batch` 限8 KiB，包含组身份、外层调用批准指纹、execution/recovery 来源及可空的已终止组运行/有序成员证据；与单文件 `patch` 互斥。它不占公开结果预算、不进模型 wire；所有身份、成员顺序、公开摘要和真实结果的对应关系由同一规则在线及 Replay 校验。运行原因与效果独立：全部已写仍可能因取消或超时终止；partial 停止当前 Turn，unknown 进入 interrupted。拒绝组无运行，可让模型解释拒绝。

准备/执行沿用原 Turn 超时，review 也受持久剩余时间约束，提交审批前再检查。暂停、重开和恢复不刷新时限。结果超限、返回错误或 Session 存储故障不能抹除副本已发生效果；回滚或丢失提交确认均按持久事实结算，不能据异常认定未写。

### 重开与已知限制

未过期 WAITING 保留供宿主决定；已消费状态只核对原组，不 prepare/save/reply/execute，不自动继续模型。既有 ToolResult 不重复观察。缺失端口、原完整计划、匹配后端决定、存储事实或归因不足保持 unknown。取消 WAITING（即使已有 Session 决定但后端未镜像）也可能为 interrupted/unknown，不自动补批以获得“未应用”结论。

修复了重开缺少原单文件/整组专用端口时，在 WAITING 内用 unknown_tool 反复尝试结算直到超时的问题；现在立即走保守恢复。旧单文件专用准入、工具定义和后端未扩大权限。

c3b 不放宽只读 Artifact 发布器；下一片 c3c 才增加基于完整计划与历史效果的专用 Diff 准入。Shell、源目录合入、创建/删除/重命名及自主 Coding Eval 均不在本片。决策见 [ADR 0035](adr/0035-kernel-batch-approval-and-recovery.md)。

## 26. 0.5.3c3c1 当前交付：真实账本绑定的差异报告

`batch_diff_document(workspace, prepared, operation, output=None, options=None)` 是纯展示器，校验私有完整镜像与输出路径/摘要对应关系；`output=None` 为计划视图，提供已结算公开效果为历史视图。它本身不能认证调用或批准，不能直接作发布准入。

`ManagedPatchBatchBridge.diff(call, scope, plan, cancel, *, view="plan", approval=None, execution=None, options=None)` 是受信宿主报告入口。plan 视图不允许混入批准或运行；effect 视图必须有完整原计划、匹配后端镜像的宿主批准，以及精确匹配 `get_execution` 的快照。运行 started 拒绝，不自动 reconcile。effect 无运行仅在批准/拒绝已镜像且账本确实未开始时成立；证明不足、镜像损坏或运行变化均明确拒绝，不新建计划或自动选择更新后的事实。

返回 `PreparedBatchDiffDocument`：完整 plan/approval/execution 与 `BatchDiffDocument`，默认 repr 隐藏全部载荷。正文通过 `document.to_jsonl()` 获取，尚不是 ArtifactRef。报告正文不含私有调用/成员 ID 或批准身份，但包含所选代码片段；不能将“未泄露凭据”理解成代码内容已脱敏。

JSONL 三种记录：一个 summary、全部有序 file、所选编辑前缀 edit。计划选择全部成员，历史效果只选择 applied/observed_after；unknown 和未执行成员只有独立文件说明。每条≤24 KiB、整体≤1 MiB，默认64 KiB；预览0–4096 UTF-8字节、默认1024。预算至少要容纳全部成员说明，不能把未知后缀截掉；编辑可按前缀截断，保留完整长度/SHA及截断标记。`complete` 是该视图编辑和文本的完整性，不是效果已知、组运行成功或批准凭证。

生成复用原精确编辑区间迭代器，不重跑新的匹配算法，不读取目标或写观察事件；目标后来变化仍可展示此前已归因历史。异步入口继续使用原桥接锁、操作时限和排空规则。新示例 `examples/batch_diff.py` 从真实 Kernel 审批取得计划/决定/效果，验证计划展示不写、实际批准写入后展示历史、Session 事件与源目录不因报告改变。

本片不变更 Agent v7、Session migration8、副本v3、Provider v3 或旧工具定义/Schema。后续 c3c2 按 ADR0037 接入独立计划/效果引用与事务发布（见第27节）。原只读 Artifact 的成功结果准入和用途内唯一性保持，不直接放宽只读发布器。

## 27. 0.5.3c3c2：计划与效果报告事务归档

实现与边界见 [ADR 0037](adr/0037-batch-diff-transaction-publication.md)。宿主显式配置：

```python
artifacts = SQLiteArtifactStore(session)
batch_diffs = SQLiteBatchDiffPublisher(artifacts, bridge)
# bridge 是同一受管副本的 ManagedPatchBatchBridge；不是模型提供的计划。
runtime = AgentRuntime(session, provider, patch_batches=bridge, batch_diffs=batch_diffs)
```

若模型需要分页读取，同一工作区的 `CodingToolRuntime(..., artifacts=artifacts)` 通过原 `scoped_tools` 接入。归档和读取独立启用；不开放通用写发布或任意 Shell。

- 发布器只接受真实事件批次，先验证完整原批次、实际未结算调用及原批准，再通过原桥接读取历史镜像；没有接收任意正文的公开发布参数。
- 整组审批的 `diff_artifact` 与审批请求/WAITING 同事务；ToolResult 的独立 `diff_artifact` 与原结果/私有效果同事务，包括恢复的终态批次。
- `ToolResult.output` 仍是原 `ManagedPatchBatchOutput`。引用进入公开输出预算和供应商历史；`patch_batch`、批准人和私有身份不进 wire。拒绝/失败/部分/未知保留原 outcome，报告不等于执行成功。
- 报告正文或引用预算不足、配额耗尽、历史报告不可用时省略引用，保留原事实；事务失败回滚正文，再结算原事实。提交后丢确认按原事件身份认领已提交结果，不重复归档。若 Session 自身不可用仍明确失败。
- 重启不执行写工具；已提交引用不重新生成。未提交效果引用可随原效果只读核对结果归档；没有完整证据时仍 unknown，不为报告补批或伪造运行。
- 同调用两个用途各自唯一；旧只读 `tool_result` 用途仍唯一。读取、清理核对用途对应的真实引用/工作区，复用原页限制、TTL、逻辑配额和活跃 Thread 保护。

当前 Agent Event/Thread **v8**、Session **migration9**。旧 v1–v7 Schema/事件字节不变，缺引用的老 Item 不增加 null 字段；旧等待审批的原完整指纹不变。原工具定义、Provider v3、副本v3及依赖不变，包版本仍0.1.0。

这完成 0.5.3c 范围的多文件报告交付，不代表整个0.5或完整生产 Coding Agent。下一阶段 **0.5.4 Process / Git / 测试执行**：先验证受管副本内非交互进程的 cwd/argv/环境准入、独立双流预算、取消/超时和进程组回收，再接入有持久准入的工具与测试反馈。POSIX 进程组不是 OS Sandbox，不能把命令执行当只读，也不能因 PID 消失推断外部副作用未发生。源码合入、完整仓库修复 Eval 仍待独立验收。

## 28. 0.5.4a：受信宿主进程运行层

实现见 [ADR 0038](adr/0038-host-process-lifecycle.md)。`processes/contracts.py` 定义独立v1请求、资源策略、二进制流和结果，`capture.py` 区分退出/管道终止，`runtime.py` 管理单事件循环内的一次执行。没有新模型工具、审批事件或数据库表。

```python
async with HostProcessRuntime(
    trusted_cwd,
    {"python": trusted_python_executable},
    limits=ProcessLimits(stdout_bytes=24576, stderr_bytes=24576),
) as host:
    result = await host.run(
        ProcessRequest(program="python", arguments=("-I", "-c", "print('hello')")),
        CancelToken(),
    )
```

构造时由宿主选择绝对cwd和可执行文件，并记录身份；请求只选择固定程序名、argv和可缩短的超时。这里的信任包括完整参数与执行代码，不是“登记Python就允许模型执行任何Python”。复核是变化检测，不是OS隔离/原子exec。只验证本地文件系统上的macOS/Linux；创建或不可中断内核回收不宣称硬实时截止保证。

默认请求30秒、宿主上限300秒，每流捕获24KiB，合计观察到8MiB触发关闭管道并终止，TERM宽限0.2秒、退出后管道排水0.5秒。阈值最后一个已交付块可能越过上限；不是内核写出配额。忙时立即 `process_busy`，不隐藏排队。主程序不搜索PATH，stdin为DEVNULL，额外FD关闭，新POSIX会话/进程组；环境只取固定默认或完整显式允许列表，不合并父环境。

`ProcessStream.data_base64`是精确保留的原始字节前缀，`data()`解码bytes，`text()`严格UTF-8；截断在多字节字符中间时明确解码失败，不悄悄替换。`observed_bytes/observed_sha256`描述已观察字节，`truncated`描述前缀丢弃，`eof`只在自然结束时为真。未EOF不宣称摘要覆盖整条输出；二流没有全局顺序。

`ProcessResult.returncode`、`stop_reason`、`termination`分别描述直接子进程返回码、停止原因、组信号阶段。非零/信号退出不吞输出；退出码0也不证明测试通过或没有后代。即使主进程已退出、后代关闭了输出，也清理原组；忽略TERM的组成员升级KILL。直接子进程由asyncio回收；脱组后代持有管道时只在排水上限后关闭读端，保留非EOF标记。

Token取消返回已启动进程的cancelled结果；Task取消/外部超时必须排空后传播。启动中取消不丢失句柄；重复取消/关闭均回收直接子进程。组信号失败返回cleanup_failed并关闭后续准入，不声称所有后代已清理。

**本切片当时仍待完成**：宿主SIGKILL/硬崩溃后的自动清理、脱组后代containment、持久命令准入、模型工具接入、Git/run_tests与Process Artifact。下节b1已交付Action Plane准入，其余边界未变。100个基础验收包含一项真硬退出的反例：没有parent-death/外部容器时子进程仍活，测试自身负责清理。这是边界证明，不是恢复成功。本片不提升Agent v8、Session migration9、Provider v3或副本v3。

0.5.4b1已接Action Plane持久命令状态；后续b2设计Agent绑定及宿主死亡运维处置，0.5.4c接Git/测试工具，不将测试或Shell变成READ_ONLY。0.5.5仍须独立真实缺陷修复Eval，之后才可对完整编码闭环作验收声明。

## 29. 0.5.4b1：Action Plane持久命令准入

实现见 [ADR 0039](adr/0039-process-action-plane-admission.md)。宿主用`process_action_tool(factory)`显式注册高风险、非幂等写Action；默认Bootstrap和Agent工具清单不注册。命名程序/argv/超时进入已有Action请求和Effect Journal，必须带幂等键并经过Policy/Approval，批准后才执行。

工具版本绑定cwd、程序身份、环境和资源策略摘要；每次执行前复核持久描述、当前Executor和新Runtime，配置漂移不消费旧权限。确定结果保存完整ProcessResult及摘要Receipt；非零退出不是传输失败。管道证据不完整或清理失败保守UNKNOWN。Task取消先回收再传播，未写终态的RUNNING由租约恢复UNKNOWN；宿主硬退出同样不自动重放、不按历史PID发信号，对账只转人工处置。

本片复用0.1 Action Plane，没有新命令账本、数据库迁移或第二审批真相。它仍不是模型Shell：Agent Session绑定、Process Artifact、硬退出自动清理与OS隔离待b2/0.7，Git/run_tests待0.5.4c。

## 30. 0.5.4b2a：Agent与Action单一审批Saga设计

决策见 [ADR 0040](adr/0040-agent-process-action-saga.md)。进程执行只认Action Journal的ApprovalRecord；Agent审批Item是绑定Thread/Turn/Call和Action身份的只读投影，不能成为第二份执行许可。稳定Action ID/幂等键确保prepare崩溃后只取得原意图。

跨库不伪装原子事务：Action已决定而Session未投影时只读取并补投影；Action运行或终态而Session无结果时只观察原Action；UNKNOWN绝不回READY。持久WAITING_ACTION用于已批准但尚未终态的Worker执行，不能在审批答复中无限轮询。

Process Artifact只负责模型展示，Action Result才是效果事实；发布失败不能改写执行终态。b2a仅冻结设计，b2b再做事件/迁移/旧reader，b2c实现运行时和Artifact。当前Agent v8/Session migration9不变，不能把本设计写成已接模型。

## 31. 0.5.4b2b1：稳定Agent调用与Process Action身份

`processes/bridge_contracts.py`定义无Agent运行时依赖的持久计划，`processes/agent_bridge.py`实现纯准备/核对边界；二者都不写Session、Journal或审批，也不启动进程。宿主传入当前Process Action的持久`ToolDescriptor`和受信`Principal`；桥接先核对完整模型ToolCall、作用域、严格Process JSON及高风险非幂等策略，再构造唯一Action请求。

`AgentProcessCallPlan`绑定Thread/Turn/Call、绝对工作区、完整调用指纹、Action请求指纹、工具版本、宿主绑定摘要、主体摘要、程序、argv摘要和超时。Action ID由固定命名空间及上述身份生成，租户范围幂等键使用同一身份；同一调用重做prepare得到完全相同请求，不同主体、命令或宿主绑定得到不同Action。完整argv只保留在原ToolCall与ActionRequest，计划只保存摘要。

`process_snapshot_matches`重新生成并逐项比较计划、ActionRequest、Action请求指纹、持久ToolDescriptor及Action Approval指纹。Session后续只能投影通过该核对的Journal快照；本片不接受Session审批作为Executor输入。计划已冻结独立v1 Schema。下一片b2b2新增Agent Event v9、WAITING_ACTION和最低Session reader升级；在此之前默认Agent仍不暴露`host.process`。
