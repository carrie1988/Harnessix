# Harnessix Code

**面向生产级、本地优先、模型无关的 Coding Agent。**

Harnessix Code 的目标是面向真实软件仓库完成代码理解、修改、命令执行、测试和交付，并把 Agent Loop、模型适配、Context、工具、会话恢复、权限、Sandbox 和外部副作用治理纳入同一个可观测、可测试的运行时。

> 当前状态：已完成 0.1 Action Plane、0.2 架构基线、0.3 Agent Runtime Kernel、0.4.1/0.4.2a 双 Adapter、0.4.2b1/b2 尝试账本、0.4.3a 成本报告，以及 0.4.3b1/b2 受控 Smoke、白名单诊断与响应计费元数据的离线验收。百炼北京文本、内存工具、审批重开实测通过，计费适用性仍待验收。0.5.1/0.5.2a 已实现工作区绑定、目录分页、文件读取和有界搜索；0.5.2b1/b2 已接通可信执行上下文和事务 Artifact，0.5.2 范围完成。0.5.3a/b1 已实现只读计划及受管副本内的持久审批、单文件真实写入和崩溃核对；0.5.3b2a 已增加调用绑定、宿主审批桥接和异步取消/恢复。0.5.3b2b 已接通显式 Kernel Patch 端口、持久写审批、SDK 离线写闭环与双账本恢复。0.5.3c1 与 c2 已实现整组计划、结构化 Diff、事务预留/审批、顺序一次性执行和部分/未知效果恢复。0.5.3c3a/c3b 已实现整组调用绑定、显式 Kernel 批量工具、持久审批重开、双 SDK 离线写闭环和双账本恢复；Diff Artifact、Shell、编码 Eval 与 Agent CLI 尚未完成，当前仍不是完整 Coding Agent。

```text
              CLI / TUI / SDK / IDE
                       │
             Versioned Agent Protocol
                       │
    ┌──────────────────▼──────────────────┐
    │           Harnessix Code             │
    │ Agent Runtime / Model / Context      │
    │ Coding Tools / Session / Sandbox     │
    └──────────────────┬──────────────────┘
                       │
    ┌──────────────────▼──────────────────┐
    │       Harnessix Action Plane         │
    │ Policy / Approval / Effect Journal  │
    │ Idempotency / UNKNOWN / Reconcile   │
    └──────────────────┬──────────────────┘
                       │
               Local OS / MCP / SaaS / DB
```

## 项目边界

Harnessix Code 自研 Coding Agent 的关键运行语义：

- Agent Loop 与 Thread/Turn/Item 生命周期；
- Provider 无关的流式模型事件；
- Context 构建、Token Budget、裁剪和 Compaction；
- Coding Tool Runtime、Process Runtime 和 Workspace 边界；
- Session 持久化、取消、恢复和双向客户端协议；
- Permission、Approval 与 Action Plane；
- MCP、项目指令、Skills 和 Hooks；
- Coding Evals、故障注入和质量回归。

Harnessix Code 复用模型供应商 SDK、OpenTelemetry、SQLite/PostgreSQL、Git、系统搜索工具和成熟 Sandbox，不重新实现已有标准与底层系统能力。LangGraph 等框架只作为可选 Adapter，不作为核心 Agent Loop。

第一版目标是 macOS/Linux、本地优先、CLI + Headless App Server。IDE、Web、多租户云平台和分布式 Agent Worker 在 1.0 之后评估。

## 当前已实现：只读编码工具与事务 Artifact

- `CodingToolRuntime` 对接既有 Kernel，提供 `list_files` / `read_file` / `glob` / `grep`；
- 0.5.2b1 新增显式 Scoped 入口：Kernel 注入 Thread/Turn/Call 归属，旧接口继续兼容；
- 根身份、拒绝路径和工具契约绑定到持久版本/审批指纹；
- 目录 FD/no-follow、普通文件类型检查、链接拒绝与读取前后漂移检测；
- 严格 UTF-8、行/字节/扫描上限、分页 revision 与显式截断；
- 大小写敏感的路径通配与字面量搜索，固定忽略规则不放宽权限；扫描缺口显式计数；
- 显式开启 Artifact 后，搜索预览外记录以有界 JSONL 归档；正文、manifest 与 ToolResult 同一 SQLite 事务提交；
- `read_artifact` 按真实会话/工作区分页读取，具备 SHA 校验、配额、TTL、活跃会话保护和过期清理；
- 顺序执行、协作取消、线程与 FD 回收、SQLite 重开/Replay；
- 真实 SDK + 离线 HTTP 的搜索→revision 读取闭环，旧/Scoped 入口累计 18 个真实只读进程崩溃切点；不调用真实 API。

~~~bash
uv run python -m examples.kernel_files
uv run python -m examples.kernel_search
uv run python -m examples.kernel_artifacts
uv run pytest tests/tools tests/artifacts
~~~

上述 CodingToolRuntime 仅支持本地 macOS/Linux 只读范围，不是 OS Sandbox；不支持正则搜索、完整 gitignore、Shell、测试执行或完整 Coding Eval；模型 Patch 通过独立专用端口启用，不属于只读工具注册表。默认搜索仍只返回有界预览；Artifact 必须由宿主显式启用，单份最多 1 MiB/10000 记录，不是无限日志存储。详细输入输出、使用方式和下一阶段见 [0.5 实施设计](docs/m05-coding-tools.md)。

## 当前已实现：只读 Patch 计划准备

- 完整前镜像读取与 SHA-256、工作区/来源 revision 绑定；
- 唯一精确锚点、同一原文的非重叠编辑；保留未涉及字节、换行和 BOM；
- 计划完整性校验与来源漂移复核；不创建临时文件或修改工作区；
- 准备器仅宿主调用，不向模型广告 `apply_patch`；实际执行须转入下述受管副本协议。

~~~bash
uv run python -m examples.patch_plan
uv run pytest tests/patches
~~~

写执行门禁和当前限制见 [Patch ADR](docs/adr/0027-prepared-patch-and-write-admission.md)。

## 当前已实现：受管副本单文件 Patch（0.5.3b1）

- 明确选择源文件，导入源目录外的私有副本；只改变副本，不覆盖用户源目录；
- 私有 SQLite 持久保存计划/前后镜像、指纹绑定的批准或拒绝、写意图和结果；
- 审批答复不执行；写意图先落库，后镜像刷盘、保存临时 inode 证据后再原子替换；
- 重开只核对前镜像、归因后镜像、第三种内容、缺失或不可读取，不盲目重写；
- 单宿主锁、线程串行、来源/元数据漂移拒绝、协作取消和效果不确定分类；
- 新增 20 个真实进程退出场景，验证源文件不变和恢复不重复应用。

~~~bash
uv run python -m examples.managed_patch
uv run pytest tests/patches
~~~

**范围边界**：这是宿主执行后端，不是模型可调用的写工具或完整编码 Eval；默认 Kernel 仍只读。副本最多 256 个文件、每文件 1 MiB、总计 32 MiB；不是完整 Git worktree，不运行钩子/代码，不自动合入源目录。私有目录和锁不等价于 OS Sandbox，也不能约束同 UID 的恶意进程。b2 已进一步交付下述宿主桥接 b2a 和 Kernel 写审批/模型工具 b2b。详见 [受管执行 ADR](docs/adr/0028-managed-patch-execution.md) 与 [实施设计](docs/m05-coding-tools.md#17-053b1-当前交付受管单文件执行)。

## 当前已实现：Patch 调用绑定桥接（0.5.3b2a）

- 复用 ToolCall/执行作用域，将 Thread/Turn/Call、提案、受管副本和不可变计划绑定；
- 稳定请求找回原计划，不在恢复时重新计算前后镜像；写审批指纹与旧只读指纹分离；
- `ManagedPatchBridge` 提供 prepare/review/execute/recover，私有计划证据与模型结果分离；
- 协作取消、Task.cancel、外层超时和重复取消均等待后台写收尾；取消不假报文件回滚；
- 新增 12 个桥接真实进程退出场景：恢复只加载/观察，不准备、批准或再次写入。

~~~bash
uv run python -m examples.patch_bridge
uv run pytest tests/patches
~~~

**分层边界**：桥接本身不读取 Session，不验证活跃 Turn 或审批时限，也不实现通用 ToolRuntime；这些责任由下述专用 Kernel 接入承担。宿主桥接示例不是自主编码 Eval。见 [ADR 0029](docs/adr/0029-managed-patch-agent-bridge.md)。

## 当前已实现：Kernel 受管写闭环（0.5.3b2b）

- 宿主显式配置 `AgentRuntime(..., patches=bridge)` 才开放 `apply_patch`；默认仍只读，任意通用写工具仍拒绝；
- 独立写审批绑定调用、提案、副本和不可变计划；答复仅落库，显式继续且持久消费等待边界后才写入；
- 替换前后取消、超时和关闭先排空线程，再分别记录工具效果与 Turn 状态；已发生的写入不假报回滚；
- Session × 副本真实进程退出后只核对，绝不重放模型/写入；不充分证据保持 unknown；
- 两个真实供应商 SDK 使用离线 HTTP，完成读取→提案→审批重开→写入→读回→回答；私有效果证据不进入模型 wire；
- 本节交付时为 Agent v6 / Session migration7；当前为 v7 / migration8，兼容 v1–v6 原文和真实旧只读/单文件等待审批。

~~~bash
uv run python -m examples.kernel_patch
uv run pytest tests/patches/test_kernel_patch*.py
~~~

范围仍为私有受管副本内的单文件精确编辑；不运行仓库代码、不合入源目录，不等于 OS Sandbox 或自主编码 Eval。接入、恢复和升级见 [ADR 0030](docs/adr/0030-kernel-managed-patch-admission.md)、[使用设计](docs/m05-coding-tools.md#20-053b2b-当前交付kernel-受管写闭环)。0.5.3c1 的只读整组准备与计划 Diff 见下节；c2a/c2b 已交付整组预留、审批、顺序消费与部分效果；c3a/c3b 已实现宿主桥接与 Kernel/模型批量闭环；c3c 再交付 Diff Artifact。

## 当前已实现：多文件计划与结构化 Diff（0.5.3c1）

- 有序、唯一的多文件提案；复用单文件精确准备器，统一工作区/提案/完整镜像与整组指纹；
- 准备完成后逐项重新复核来源，共用取消/截止时间；不修改文件或持久化半组计划；
- 输出精确编辑的前/后 UTF-8 字节坐标、片段长度/摘要与有界预览，保留 BOM、CRLF 和未涉及字节；
- 按实际 JSON UTF-8 字节预算返回前缀，缺失编辑或文本截断均明确标记，不冒充完整 Diff。

~~~bash
uv run python -m examples.patch_batch
uv run pytest tests/patches/test_batches.py tests/patches/test_diff.py
~~~

**边界**：仅宿主只读计划与展示，不是整组批准/执行或 git apply 补丁；不自动发布 Artifact、回灌模型或合入源目录。首版最多16文件、提案文本合计512 KiB、完整前后镜像合计8 MiB。原模型 apply_patch 仍为受管单文件。设计及后续 c2/c3 门禁见 [ADR 0031](docs/adr/0031-patch-batches-and-structured-diff.md)。

## 当前已实现：整组预留与持久审批（0.5.3c2a）

- 一次事务保存整组及全部成员，统一检查既有计划/镜像配额和组元数据预留；
- 批准绑定副本、宿主稳定请求、有序完整计划及组指纹；相同请求/决定幂等，内容冲突拒绝；
- 副本账本 v1→v2 事务升级保留旧事件/镜像，旧 reader 拒绝新格式；
- 组批准不转换成成员单独批准，旧单文件入口不能拆分消费；重开仅查询，无补丁写入。

~~~bash
uv run python -m examples.managed_batch_approval
uv run pytest tests/patches/test_managed_batches.py tests/patches/test_batch_crash.py
~~~

**c2a 历史边界**：组批准本身不执行；c2b 新增的显式 execute/get_execution/reconcile 见下一节。c3 的 Kernel 批量工具和 Diff Artifact 尚未实现。预留设计见 [ADR 0032](docs/adr/0032-durable-batch-reservation-and-approval.md)。

## 当前已实现：多文件一次性执行与恢复（0.5.3c2b）

- 整组开始记录落库即消费批准；先整组复核，再严格逐成员执行，复用原单文件写引擎；
- 第一处失败、取消或未知效果立即停止后续调度；已成功文件保留，未开始成员不伪装执行失败；
- 区分全未应用、全已应用、已知部分和含未知效果，文件效果与 completed/cancelled/timeout/failed/interrupted 分开；
- 重开只核对已有成员，绝不重放写入；即使全部文件已改完，崩溃恢复仍标记 interrupted；
- 副本账本 v3 独立迁移，旧事件、组计划和审批 Schema 保持不变。

~~~bash
uv run python -m examples.managed_batch
uv run pytest tests/patches/test_batch_execution.py tests/patches/test_batch_execution_crash.py
~~~

**边界**：仅宿主显式调用、仅私有受管副本内的已有普通文件。不承诺跨文件原子提交、内容 CAS 或自动回滚；不合入源目录，不运行 Shell。c2 本身不开放模型写工具；当前批量 Kernel 接入见 c3b，Diff Artifact 仍待 c3c。设计见 [ADR 0033](docs/adr/0033-batch-consumption-and-effect-recovery.md)，迁移见 [部署说明](docs/deployment.md#副本账本-v3-升级053c2b)。

## 当前已实现：整组调用绑定与异步桥接（0.5.3c3a）

- 独立完整调用计划绑定 Thread/Turn/Call、工具/提案、副本和全部有序成员；旧单文件或后端批准不能替代调用批准；
- `ManagedPatchBatchBridge` 提供 prepare/review/execute/recover，复用现有组后端，不新增替换器或数据库格式；
- 取消、超时、排队和重复关闭均排空活动线程；公开效果与私有批准/运行证据分离；
- 重开必须核对原完整计划和宿主决定，缺少或损坏证据保持 unknown，不自动补跑。

~~~bash
uv run python -m examples.batch_patch_bridge
uv run pytest tests/patches/test_batch_bridge.py tests/patches/test_batch_bridge_cancel.py tests/patches/test_batch_bridge_crash.py
~~~

**边界**：这是受信宿主 API，本身不验证 Session 活跃性或持久消费；宿主必须使用原 Turn 截止时间和取消机制。当前通过下节 c3b 的专用端口接入 Kernel；默认仍不开放写工具，c3c 的 Diff Artifact 尚未实现。详见 [ADR 0034](docs/adr/0034-batch-call-bridge-and-kernel-integration.md)。

## 当前已实现：Kernel 整组持久审批与写闭环（0.5.3c3b）

- 宿主显式注入 `AgentRuntime(..., patch_batches=ManagedPatchBatchBridge(copy))` 才广告/执行 `apply_patch_batch`；与原 `patches` 单文件端口可共存，不开放任意写注册；
- Session 保存完整调用计划、独立组审批与决定；持久离开等待后才镜像后端决定并一次性顺序执行。答复审批不会修改文件；
- 两个实际供应商 SDK 均通过离线 HTTP 完成“两文件读取→整组提案→审批重开→真实副本写入→逐文件读回”；没有新增真实模型调用；
- 私有 `ToolResult.patch_batch` 保留有界效果与运行原因，不进模型 wire，也不因公开结果超限丢失。部分效果停止当前 Turn；未知效果禁止自动继续；
- Agent Event/Thread **v7**、Session **migration8**；真实旧 v6 wheel 的只读/单文件审批升级通过，旧事件/投影原字节不重写，旧 reader 明确拒绝新库。副本账本保持 **v3**。

~~~bash
uv run python -m examples.kernel_batch
uv run pytest tests/patches/test_kernel_batch*.py tests/agent/test_batch_session_upgrade.py
~~~

这是已有普通文件的受管副本闭环，不是跨文件原子提交、源目录合入、OS Sandbox 或自主编码 Eval。取消等待或后端未镜像决定时，证明不足仍保守记为 unknown，不补批/重放。下一片为 **c3c：计划与历史效果 Diff Artifact**。详见 [设计](docs/m05-coding-tools.md#25-053c3b-当前交付kernel-整组持久审批与恢复)、[ADR 0035](docs/adr/0035-kernel-batch-approval-and-recovery.md) 和 [测试记录](docs/testing-and-evals.md#27-053c3b-kernel-整组闭环验收2026-09-04)。

## 当前已实现：0.1 Action Plane

- Python 3.12+、asyncio、Pydantic v2、FastAPI；
- 版本化且框架无关的 `ActionRequest`；
- 运行时拥有的 Tool Schema、副作用类型和风险等级；
- `allow`、`deny`、`require_approval` 策略结果；
- SQLite 与 PostgreSQL 当前快照、追加式 Effect Journal；
- 租户范围幂等键和载荷冲突检测；
- `READY → LEASED → RUNNING` 执行租约；
- 基于 Journal `READY` 状态的持久队列；
- API 与独立 Worker 进程解耦；
- Worker 心跳续租、Owner 校验和过期恢复；
- PostgreSQL `FOR UPDATE SKIP LOCKED` 多 Worker 原子 Claim；
- W3C Trace Context 经 Journal 跨进程持久传播；
- OpenTelemetry Trace/Metrics 可选适配器与 NoOp 默认实现；
- JSON 结构化日志、队列指标和 `/readyz` 就绪检查；
- 显式 `UNKNOWN`，写操作异常默认不盲目重试；
- Executor 专用 `reconcile()` 对账契约；
- FastAPI、同步/异步 Python SDK；
- LangGraph/LangChain `StructuredTool` 适配器；
- `system.echo` 与 `demo.issue.create` 两个可运行 Executor；
- 不确定副作用注入和无重复对账测试。

## 当前已实现：0.3 Agent Runtime Kernel

- Thread/Turn/Item/AgentEvent、纯 Reducer 和版本化 JSON Schema；
- Event Log 与聚合快照原子提交、sequence CAS 和请求幂等；
- 供应商中立的 ModelProvider/ToolRuntime 端口；
- Fake/Scripted Provider、多步骤只读工具循环；
- 步数、报告 Token 用量、时间和输出大小边界；
- 用户取消、Task 取消、流清理和单 Runtime 宿主锁；
- 持久审批暂停、答复、取消、指纹校验与显式继续；
- 重启保留审批检查点，其他中断步骤显式 INTERRUPTED，不自动重放工具；
- Plan/Compaction/Error 语义 Item 和统一错误分类；
- Agent OTel Trace/Metrics、审批重启关联与可观测性故障降级；
- 版本化 Agent Event、Session 历史迁移，旧事件不改写（当前 v7，见上述整组写工具接入）；
- SessionStore 共享契约和损坏/不可写/磁盘满等故障测试；
- Transcript Replay、投影重建和真实进程故障注入。

离线验收：

~~~bash
uv run pytest tests/agent
uv run python examples/kernel_replay.py
uv run python examples/kernel_approval.py
uv run --extra observability python -m examples.kernel_observability
~~~

Plan/Compaction 当前支持可信宿主记录与 Replay，不包含自动规划或压缩算法。

这些入口验证真实 Kernel 和 SQLite 持久化，不调用模型 API，也不代表已经具备真实编码能力。当前仅允许可信只读 Tool，包括需要审批的只读调用；写工具仍关闭。审批为进程内接口，不是客户端审批 UI；完整边界与剩余任务见 [Kernel 实施设计](docs/m03-runtime-kernel.md)。

## 当前已实现：0.4.1 / 0.4.2a Model Provider

- 可选官方 OpenAI Python SDK，Kernel 不导入供应商类型；
- Chat Completions 文本/工具分片、真实 Usage 和 Stop Reason 归一化；
- 工具名称别名、跨步骤 Call UUID 配对与审批重启继续；
- 显式能力、Secret 环境引用、HTTPS/无重定向/无环境代理；
- 首语义事件前有界重试，中途断流不重放；取消和错误 body 均关闭响应；
- 请求/响应/帧大小、输出和超时边界；默认 CI 无真实凭据。
- 可选 Anthropic SDK/HTTPX2 Adapter，共享契约、缓存计数合入输入总量及跨 Provider 会话/审批继续。

~~~bash
uv sync --locked --all-extras --dev
uv run pytest tests/models
uv run --extra openai python examples/kernel_openai_offline.py
uv run --extra anthropic python examples/kernel_anthropic_offline.py
~~~

以上命令使用真实 SDK + HTTP 替身。另行完成的百炼北京三场景实测与首次工具失败/修复记录见 [真实验证记录](docs/validation/bailian-2026-09-03.md)，**不代表所有平台、模型或真实编码场景均通过**。OpenAI Adapter 仅支持显式配置的 Chat 兼容协议，不声称支持所有模型、Responses 或原生推理功能。API 使用、能力边界和后续验收见 [Model Runtime](docs/m04-model-runtime.md) 与 [ADR 0014](docs/adr/0014-openai-compatible-provider.md)。

Anthropic 当前是非 Thinking 的 Messages 配置，要求完整缓存计数，不开放签名推理块、服务器工具或 Fallback；同样尚未做真实平台验证。设计与限制见 [ADR 0015](docs/adr/0015-anthropic-provider.md)。

## 当前已实现：0.4.2b1/b2 模型尝试账本与 SDK 接入

- 每次尝试的持久意图、响应身份、累计用量观测和完成/失败/取消/中断事实；
- unknown/partial/complete 用量，缓存与推理子集不重复加总，未知值不填零；
- 重复累计观测、最终响应与重试共用一份预算记账；
- 失败/取消保留已知用量，进程恢复不重发模型请求；
- 当时交付 Agent Event/Thread v4、Provider Event v2、真实 v1/v2/v3 会话升级与冻结 Schema（当前为 Agent v7/Provider v3，见 0.5.3c3b 与 0.4.3b2）；
- 两类实际 SDK 在 HTTP 前发布尝试意图，重试使用独立 UUID，不把意图当作已收费；
- 缓存读取/创建与公开推理计数映射、响应失败时保留最后合法观测；
- 当时交付 23 个模型尝试相关子进程崩溃切点，全项目合计 49 个；0.4.3b2 后分别为 28 / 54 个；差额 Token 指标。

两个 SDK 均已使用当前 Provider v3 尝试元数据（兼容原 v2 尝试语义）；旧自定义 Provider 的响应记账路径保持兼容。`Turn.usage` 只是已知消费下界，需同时查看 `usage_is_complete`，缺失分项仍为 null；它不是成本或供应商账单。价格估算已在 0.4.3a 实现，真实验证待续。设计见 [ADR 0016](docs/adr/0016-model-attempt-ledger.md) 与 [ADR 0017](docs/adr/0017-provider-attempt-usage.md)。

## 当前已实现：0.4.3a 版本化 Token 成本报告

- 显式绑定价格快照、实际模型和宿主核对的计费上下文，不按 Adapter 类型猜平台；
- 支持同价/缓存分项输入、包含推理的输出、输入长度阶梯、生效区间与 TTL 条件；
- 采用整数定点运算，不用浮点金额，不把缺失用量或上下文算作零费用；
- 失败尝试的完整用量可计价；跨重试只计一次，USD/CNY 分开汇总，不隐式换汇；
- 独立报告保存价格与必要用量事实，JSON 重载重算；不修改会话历史或复制 Prompt/错误原文。

~~~bash
uv run --extra openai python -m examples.kernel_cost_offline
~~~

入口使用真实 SDK/Kernel 与 HTTP、价格、计费上下文夹具。当前是**事后 Token 估算**，不是自动采集的计费账本、实时费用硬上限或实际账单；真实平台费率没有内置。详见 [ADR 0018](docs/adr/0018-versioned-token-cost.md)。

## 当前已实现：0.4.3b1 受控模型 Smoke

- 显式启用才创建 SDK/读取凭据；固定文本、内存工具、审批重开三场景；
- 复用真实 Kernel/SQLite/Replay，不读取业务工作区，不执行 Shell 或文件修改；
- 最多两个模型步骤、不重试，配置受限；JSON 报告不复制端点、Prompt、模型/响应 ID 或错误原文；
- 两个真实 SDK 的离线传输验收、失败/超时/取消和诊断 canary 已覆盖；不等同于真实平台验证。

~~~bash
uv run harnessix model-smoke --help
uv run pytest tests/smoke
~~~

操作说明、退出码、凭据引用和隐私边界见 [Smoke 使用说明](docs/model-smoke.md)。Token 检查不等于金额硬上限；响应计费元数据已在 0.4.3b2 接入，但不自动识别平台计价规则。

## 当前已实现：0.4.3b2 响应计费元数据

- 服务等级、推理地域、5m/1h 缓存写入分项与 Usage 原子持久化；缺失保持未知，重复不重计、漂移拒绝；
- OpenAI/Anthropic SDK 映射、失败/取消/崩溃保留与 Replay；
- Agent Event/Thread v5、Provider Event v3，真实 v1–v4 混合升级，旧 Schema 不改写；
- 仅对宿主明确声明的匹配直连平台映射计价上下文；代理/百炼不自动套用原生价格；
- 混合/不完整 TTL 不强行选单一费率，价格绑定拒绝与已观测事实冲突；CostReport v1 保持重算兼容。

设计见 [ADR 0020](docs/adr/0020-observed-billing-context.md)。0.4 整体验收和真实编码工具仍未完成。

## 当前 Action Plane 快速开始

环境要求：Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
make install
make check
make run
```

服务默认监听 `http://127.0.0.1:8787`，交互式接口文档位于 `http://127.0.0.1:8787/docs`。

在另一个终端运行 Action Plane 可靠性演示：

```bash
make demo
```

演示流程包括：

1. 执行只读 `system.echo`；
2. 提交 `demo.issue.create` 并进入审批；
3. 批准后模拟“外部 Issue 已创建，但本地结果丢失”；
4. Action 进入 `UNKNOWN`；
5. 对账器按业务幂等键查到既有 Issue；
6. Action 变为 `SUCCEEDED`，不重复创建 Issue。

上述演示使用默认 `inline` 模式，适合本地调试。

## 队列执行模式

生产形态使用 PostgreSQL，并将 API 与 Worker 分开启动：

```bash
export HARNESSIX_DATABASE_URL='postgresql://harnessix:***@数据库地址:5432/harnessix'
export HARNESSIX_EXECUTION_MODE=queued

# 终端一：只负责接收、校验、策略和审批
uv run harnessix serve

# 终端二：Claim READY Action 并执行
uv run harnessix worker
```

在 `queued` 模式下，提交或批准 Action 后，HTTP API 返回 `202` 和 `READY` 快照；独立 Worker 完成执行后，可通过 `GET /v1/actions/{action_id}` 查询最终状态。

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `HARNESSIX_DATABASE_URL` | 空 | 配置后使用 PostgreSQL Journal |
| `HARNESSIX_DATABASE_PATH` | `.harnessix/harnessix.db` | 未配置 PostgreSQL 时使用的 SQLite Journal |
| `HARNESSIX_DEMO_DATABASE_PATH` | `.harnessix/demo-external.db` | 模拟外部 Issue 系统的独立 SQLite 文件 |
| `HARNESSIX_EXECUTION_MODE` | `inline` | `inline` 或 `queued` |
| `HARNESSIX_LEASE_SECONDS` | `30` | 执行租约时长 |
| `HARNESSIX_WORKER_POLL_SECONDS` | `0.5` | 空队列轮询间隔 |
| `HARNESSIX_WORKER_HEARTBEAT_SECONDS` | `10` | Worker 续租间隔，必须小于租约时长 |
| `HARNESSIX_RECOVERY_INTERVAL_SECONDS` | `5` | 过期租约扫描间隔 |
| `HARNESSIX_LOG_FORMAT` | `json` | `json` 或 `console` |
| `HARNESSIX_LOG_LEVEL` | `INFO` | 日志级别 |
| `HARNESSIX_OTEL_ENDPOINT` | 空 | OTLP/HTTP Collector 基础地址 |

## LangGraph 适配

```python
from pydantic import BaseModel

from harnessix import ActionContext, EffectClass, HarnessixAsyncClient, Principal
from harnessix.adapters.langgraph import HarnessixToolContext, create_harnessix_tool


class IssueInput(BaseModel):
    title: str
    body: str = ""


client = HarnessixAsyncClient()
issue_tool = create_harnessix_tool(
    action_name="demo.issue.create",
    description="创建经过治理的 Issue",
    args_schema=IssueInput,
    async_client=client,
    context=HarnessixToolContext(
        principal=Principal(
            tenant_id="demo",
            subject_id="langgraph-agent",
            framework="langgraph",
        ),
        action_context=ActionContext(session_id="thread-1", run_id="run-1"),
    ),
    effect_hint=EffectClass.IDEMPOTENT_WRITE,
    idempotency_key=lambda arguments: f"issue:{arguments['title']}",
)
```

返回的对象是标准 LangChain Tool，可直接交给 LangGraph `ToolNode`。Policy、Approval、Journal 和 Executor 仍位于 Harnessix 边界之后。

## 当前仓库结构

```text
src/harnessix/domain/       Action Contract、状态和端口
src/harnessix/storage/      SQLite/PostgreSQL Effect Journal 与迁移
src/harnessix/policy/       Policy Engine 实现
src/harnessix/executors/    内置和演示 Executor
src/harnessix/api/          FastAPI HTTP 边界
src/harnessix/sdk/          Python 同步/异步客户端
src/harnessix/adapters/     Agent 框架适配器
src/harnessix/agent/        Kernel 领域模型、Reducer、Loop、取消
src/harnessix/models/       Provider 契约、Fake/Scripted Provider
src/harnessix/session/      SQLite Session Store、迁移与宿主锁
tests/                      单元和集成测试
docs/                       中文架构与决策文档
spec/                       生成的 JSON Schema 和 OpenAPI
examples/                   可运行演示
```

后续按里程碑增量加入 `context/`、`tools/`、`workspace/`、`protocol/`、`extensions/` 和 `evals/`，不进行一次性目录重写。

## 设计资料

- [产品章程](docs/product-charter.md)
- [总体架构](docs/architecture.md)
- [主流 Coding Agent 源码研究计划](docs/research-plan.md)
- [0.2 源码研究基线](docs/research/baselines.md)
- [Agent Loop 研究](docs/research/agent-loop.md)
- [Session 模型研究](docs/research/session-model.md)
- [协议与 Provider Event 研究](docs/research/protocol.md)
- [Tool Runtime 研究](docs/research/tool-runtime.md)
- [Context Engine 研究](docs/research/context-engine.md)
- [Permission、Approval 与 Sandbox 研究](docs/research/security.md)
- [演进为 Harnessix Code 的架构决策](docs/adr/0005-evolve-to-harnessix-code.md)
- [Thread/Turn/Item/Event 决策](docs/adr/0006-thread-turn-item-event-model.md)
- [Agent Loop 与取消决策](docs/adr/0007-agent-loop-and-cancellation.md)
- [Provider Event 决策](docs/adr/0008-provider-event-model.md)
- [App Server Protocol 决策](docs/adr/0009-app-server-protocol.md)
- [Session Store 与恢复决策](docs/adr/0010-session-store-and-recovery.md)
- [威胁模型 v1](docs/threat-model.md)
- [测试与 Eval 规范 v1](docs/testing-and-evals.md)
- [0.3 Kernel 实施设计](docs/m03-runtime-kernel.md)
- [持久审批与恢复设计](docs/adr/0012-durable-approval-checkpoint.md)
- [Kernel 契约与诊断设计](docs/adr/0013-kernel-contracts-and-telemetry.md)
- [0.4 Model Runtime 实施计划](docs/m04-model-runtime.md)
- [进程内宿主与初始投影决策](docs/adr/0011-kernel-host-and-initial-projection.md)
- [Action Contract](docs/action-contract.md)
- [Action 生命周期](docs/action-lifecycle.md)
- [自研与复用边界](docs/build-vs-buy.md)
- [设计与开发路线图](docs/roadmap.md)
- [M1 Worker 与 PostgreSQL 设计](docs/m1-worker-postgresql.md)
- [M1.2 可观测性设计](docs/m1-observability.md)
- [部署与运行](docs/deployment.md)

## 目标里程碑

| 版本 | 结果 |
|---|---|
| 0.2 | 产品、源码研究与架构基线 |
| 0.3 | 可恢复 Agent Runtime Kernel |
| 0.4 | OpenAI-compatible / Anthropic Model Runtime |
| 0.5 | Read/Search/Patch/Shell/Git/Test 编码闭环 |
| 0.6 | Context Compaction 与持久会话 |
| 0.7 | Workspace、Permission、Sandbox、Action Plane 集成 |
| 0.8 | App Server、MCP、Skills、Hooks |
| 0.9 | CLI/TUI、故障注入与 Coding Evals |
| 1.0 | 生产发布 |

## 重要语义

Harnessix 不承诺任意外部系统上的神奇 Exactly Once。它提供的是：

> Action 身份稳定、可幂等时安全复用、结果不确定时停止盲目重试，并通过外部观察和对账尽量实现业务级 Effectively Once。
