# 0.5 Coding Tool Runtime 详细实施设计

- 日期：2026-09-03
- 状态：0.5.1 / 0.5.2 只读、搜索、可信作用域及有界 Artifact 已实现；0.5.3a/b1 计划及受管单文件写后端已实现；0.5.3b2/c–0.5.5 仍待实施
- 目标：从“模型调用正确”推进到“能够在真实仓库中可靠定位、修改、验证并交付”

## 1. 实际基线与不扩大的边界

当前 Kernel 已有 `ToolRuntime.definitions/execute`、`ToolDescriptor`、持久 ToolCall/ToolResult、工具版本/指纹、审批和恢复语义。`ToolCallContent` 已包含版本、Effect Class、参数及审批绑定；`ToolResultContent` 已有 succeeded/failed/cancelled/unknown 和 Action ID。不要重新命名或并行建设另一套 Call/Result。

当前 `_execute_tool` 明确拒绝非 READ_ONLY，恢复逻辑也据此判断结果。不允许仅删除这一检查就开放 Patch/Shell。0.5 分片先接入真实只读工具；本地写必须先补齐效果、并发与恢复设计。

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
| 0.5.3b2 | Kernel 模型写工具闭环 | 待实施；版本化写审批、Scoped 准入、双账本边界与恢复 |
| 0.5.3c | 多文件效果与 Diff | 待实施；部分效果、结构化交付与兼容，不能假报整体原子 |
| 0.5.4 | Process、Git、run_tests、受控 Shell | 子进程树、输出管道、取消/超时、环境和审批边界通过 |
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
../patches/        已有精确计划、受管副本及持久写执行；模型接入待 b2
processes.py       argv、进程组、并发排水、取消与清理
~~~

不预建空壳文件。0.5.1 已有前四项，0.5.2a 新增三个搜索模块，0.5.2b2 新增 artifacts 包；0.5.3a 新增 patches/contracts.py 与 planner.py。0.5.3b1 新增 managed/managed_io/ledger/managed_contracts；模型写工具/Process 尚未实现。

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

0.5.3a 的准备器仍只读；0.5.3b1 仅对工厂创建的私有副本开放宿主写 API，见第 17 节与 [ADR 0028](adr/0028-managed-patch-execution.md)。Kernel/模型写入仍未开放。

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

本片新增 `artifacts/contracts.py`、`ports.py`、`sqlite.py` 和 migration `0006_artifacts.sql`，以及六份独立 Artifact/归档输出 v1 Schema。事件和投影仍为 Agent v5，旧 migration 与八份默认只读 Schema 原样保留；**数据库 migration 6 与 Agent Schema 5 不是同一个版本号**。旧程序拒绝新库；不是可降级升级。

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

`examples.patch_plan` 验证真实 CRLF 文件的目标内容计算、源文件保持不变，以及模拟外部编辑后的复核拒绝。它不执行 Patch，也不属于自主编码 Eval。上述宿主写前置条件现由 b1 受管后端实现；模型接入仍待 b2。源目录并发编辑的无覆盖保证不能由 hash+rename 推导。


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

### 下一片 0.5.3b2 的实施顺序

实施进展：已先交付 **b2a 宿主桥接**（第 18 节），完成稳定调用绑定及异步收尾前置条件；以下完整 Agent 事件/审批/模型接入归入 **b2b**，未因桥接可运行而提前勾选完成。

1. 固化 Agent 写审批/结果契约与兼容迁移；不改义 kernel-read-only/v1。
2. 在受信 Scoped 入口绑定 Thread/Turn/Call、受管副本和持久 plan_id；模型参数不得注入授权或归属。
3. 明确模型提交提案、生成宿主计划、等待审批、消费计划、发布结果的顺序；旧只读审批仍按旧规则运行。
4. 以稳定请求 ID 连接 Session 与副本账本；覆盖“计划已保存/Session 未提交”“文件已写/ToolResult 未提交”等非原子窗口。
5. 接入最小模型 Patch 工具；专用非幂等写准入，不放开任意 NON_IDEMPOTENT_WRITE 工具。
6. Kernel 取消/超时必须回收后台写线程；重启只加载/核对已有计划，不把历史 Call 再执行。
7. 增加真实 SDK 离线读→提案→审批重开→写→读回→回答集成、跨版本旧审批，以及 Kernel × 文件替换真实崩溃矩阵。
8. 通过全量回归、独立 wheel 与 Linux/macOS CI 后，才关闭 0.5.3b；真实 API 仍需独立预算授权。

## 18. 0.5.3b2a 当前交付：宿主调用绑定桥接

设计见 [ADR 0029](adr/0029-managed-patch-agent-bridge.md)。新增 `patches/agent_bridge.py`、`bridge_contracts.py`，复用既有只读调用归属、提案准备器和 b1 受管后端。只新增两个独立 v1 Schema，**Agent v5 / Action v1 / Session migration 6 / 副本账本 schema v1 和默认工具清单不变**。`ManagedPatchBridge.definition()` 返回单一待接入写定义，不是通用 ToolRuntime；Kernel 仍拒绝该非只读调用。

### 宿主 API

| API | 本片语义 |
| --- | --- |
| `ManagedPatchBridge(copy)` | 绑定宿主已取得所有权的一个受管副本，不接受任意可写目录 |
| `definition()` | 固定 apply_patch 提案契约，non_idempotent_write、高风险、必须审批、可核对；不自动注册 |
| `prepare(call, scope, cancel)` | 验证调用/副本/严格提案；按稳定 request_id 查找原计划，仅缺失时准备并保存；返回 ManagedPatchCallPlan |
| `review(call, scope, plan, cancel)` | 仅 pending 可复核；验证保存的计划与当前完整前镜像，不记录决定、不写文件 |
| `execute(call, scope, plan, approval, cancel)` | 验证桥接审批指纹，镜像宿主决定到后端；批准走一次性执行，拒绝不改文件；已消费计划不重试 |
| `recover(call, scope, cancel, plan=None, approval=None)` | 只查找/读取/reconcile；不 prepare/save/reply/execute；可找回保存后尚未发布给 Session 的计划 |
| `aclose()` / `async with` | 排空本桥接的后台操作，拒绝后续操作；不关闭或删除宿主副本 |

后端新增 `lookup(request_id, operation)` 和 `verify(plan_id, operation)`，分别只加载既有计划及复核完整前镜像；不修改旧 get/save/execute 契约，不迁移已有副本数据库。

### 调用与私有证据

`ManagedPatchCallPlan` 包含 Thread/Turn/Call、调用摘要、稳定请求、副本/计划身份、完整 manifest、后端指纹与桥接审批指纹。最后者绑定整份计划，不等于后端指纹或 kernel-read-only/v1 的执行摘要。模型参数仅是 PatchProposal：相对路径、expected_revision、精确 edits；注入 actor、plan_id、scope、批准标志等全部拒绝。

`PatchCallResult.result` 是现有 ToolResultContent；output 仅含版本、相对路径、历史状态、前后内容 SHA。`plan` / `record` 单独留给宿主，不进入模型结果。原提案仍可能含用户代码，副本私有账本仍持有前后镜像；本片不声称代码从未进入模型/日志，宿主需保留既有数据处理边界。

### 恢复、取消与授权边界

- 缺少计划且调用方也未提供持久计划时报告未成功；提供了计划而磁盘找不到时为 unknown，不假定未执行。
- pending/approved 没有消费写意图，rejected/failed/observed_before 已知未成功；恢复不会据此自动重试。
- started/uncertain 先做归因观察。applied/observed_after 还须匹配宿主批准才能报告 succeeded；缺批准、错绑定、第三种内容、缺失、不可读、账本异常为 unknown。
- 调用契约、参数或执行作用域本身无效时直接抛出结构化错误；上条 unknown 指进入账本核对后发现的不一致，不能把入口异常自动解释为未产生效果。
- 已应用状态是历史事实；不能据此断言文件此刻未被外部编辑。执行抛错不等价于无效果，调用方必须核对，不能统一转成失败。
- 桥接使用串行线程和 ReadOperation；协作取消、Task.cancel、外层超时或重复取消必须等待写线程退出。替换前停止可未应用；替换后先完成效果与记账。close 同样等待；没有不可中断 I/O 的硬实时终止保证。
- **尚无 Session 授权核验**：ApprovalRecord 是宿主声明，本层不验证当前 Turn、截止时间或是否已消费审批。完整 Kernel 接入必须先持久审批并消费恢复边界，再调用 execute；不能把旧 READ_ONLY 审批当作写授权。

`uv run python -m examples.patch_bridge` 串联真实只读工具→精确提案→持久计划找回→宿主批准→写入→读回→重开核对。它证明桥接与既有组件互通，不是模型驱动的编码 Eval。b2b 将复用这一桥接，不另写文件替换器或恢复执行器。
