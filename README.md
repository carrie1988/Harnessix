# Harnessix Code

**面向生产级、本地优先、模型无关的 Coding Agent。**

**社区许可证：** [AGPL-3.0-only](LICENSE) ｜ **闭源商业使用：** [商业许可说明](COMMERCIAL_LICENSE.md)

Harnessix Code的目标是独立实现面向真实软件工程任务的生产级Coding Agent，在真实仓库中稳定完成理解、规划、修改、执行、验证、审查和交付，并把Agent Loop、模型适配、Context、工具、会话恢复、权限、Sandbox和外部副作用治理纳入同一个可恢复、可审计、可评测的运行时。

> 当前状态：已完成0.1～0.9.0路线图范围和DOC-1.0～DOC-1.6文档治理；81份ADR和冻结源码研究均已进入版本化文档合同。0.9.1a～d以及0.9.1e1～e5已通过对应全矩阵CI。0.9.1f1已把独立Action HTTP/Worker撤出公共产品面，f2a固定Container Process和f2b直接Trusted Git Push均已关闭；f2c/f3继续迁移历史Eval并删除兼容内核。0.9.2～0.9.6发布证据仍待完成，不能把当前版本宣称为1.0产品。当前能力、显式装配能力和规划能力以[文档中心](docs/README.md)及[总体架构](docs/architecture.md)为准。

```text
              CLI / TUI / Agent SDK
                       │
             Versioned Agent Protocol
                       │
    ┌──────────────────▼──────────────────┐
    │           Harnessix Code             │
    │ Agent Runtime / Model / Context      │
    │ Session / Artifact / Coding Tools    │
    │ Trusted Action Runtime / Sandbox     │
    └──────────────────┬──────────────────┘
                       │
        Workspace / Container / MCP / Git
```

## 项目边界

Harnessix Code 自研 Coding Agent 的关键运行语义：

- Agent Loop 与 Thread/Turn/Item 生命周期；
- Provider 无关的流式模型事件；
- Context 构建、Token Budget、裁剪和 Compaction；
- Coding Tool Runtime、[Process Runtime](docs/modules/processes.md)和 Workspace 边界；
- Session 持久化、取消、恢复和双向客户端协议；
- Permission、Approval 与 [Trusted Action Runtime](docs/modules/trusted-actions.md)；
- MCP、项目指令、Skills 和 Hooks；
- Coding Evals、故障注入和质量回归。

Harnessix Code 复用模型供应商 SDK、OpenTelemetry、SQLite/PostgreSQL、Git、系统搜索工具和成熟 Sandbox，不重新实现已有标准与底层系统能力。LangGraph 等框架只作为可选 Adapter，不作为核心 Agent Loop。

1.0目标是面向大量独立macOS、Linux和Windows终端用户安装和长期使用的本地优先正式商用版本，提供CLI/TUI、Headless App Server和Python Agent SDK。大量用户表示大量相互独立的本地实例，不表示1.0建设集中式多租户SaaS；IDE、Web、远程Sandbox、云任务和分布式Agent Worker在1.x按真实需求评估。当前0.7 Workspace Snapshot、Process/Job Object/ConPTY、受管Git交付和统一Action入口均已通过Windows原生或平台中立门禁；0.8已提供Agent Protocol、MCP/Skill/Hook和Provider/Profile产品配置边界，完整TUI和Windows发行物仍未完成，Windows产品整体未达到当前支持门禁。

## 当前已完成：0.7可信执行与工程交付

- POSIX/Windows原生Workspace Snapshot、规范路径与跨进程fencing租约；
- 不可变`ExecutionPlanV2`与精确Approval Checkpoint，绑定Tool、参数、cwd、环境、Workspace、Sandbox、网络和Secret版本；详见[Execution Plan模块设计](docs/modules/execution.md)；
- Container强隔离、选择性网络、Secret最小注入/流式脱敏，以及POSIX Process Group、Windows suspended Job Object/ConPTY和持久后台监督；
- 多文件Workspace Transaction、私有CAS、append-only账本、崩溃恢复、新事务Rollback和完整Diff；
- 受管Git worktree/checkpoint/显式commit，固定Git环境且来源HEAD/index不移动；
- `TrustedActionRouter`统一宿主Tool Binding、规范资源、Policy/Approval、摘要化审计和`UNKNOWN → reconcile`；
- `ExtensionActionPort`把MCP/Skill/Hook/custom限制为来源隔离的plan/execute/reconcile端口，不暴露executor、Session、Secret或文件系统对象；
- Git Push与Commit分离、默认不装配；Push只更新一个ref，使用exact lease，调用结果丢失后只对账不重放。

0.7.5的受控真实Push使用本地bare remote验证零重复副作用；公网HTTPS/SSH凭据不会从宿主环境隐式继承，须在0.9.5通过独立凭据作用域、known-hosts和跨平台Dogfooding后才能装配，不能复用模型Provider API Key。设计、失败语义和限制见[0.7详细设计](docs/m07-trusted-execution-and-delivery.md)与[统一Action Plane源码研究](docs/research/unified-action-plane-and-extension-boundaries.md)。

## 当前已实现：产品运行时与可信扩展（0.8）

- Agent Protocol v1使用严格JSON-RPC 2.0/stdio JSONL合同，Command的持久`requestId`与连接内JSON-RPC `id`分离；
- Headless App Server复用唯一Agent Runtime和Session Store，支持Thread创建、恢复、分叉、归档，Turn开始、重试、取消、审批、提问与Steering；
- Python Agent SDK支持进程内和子进程传输；子进程传输以唯一Reader和`id → Future`表归并乱序响应，长轮询不会阻塞控制命令；
- `events/next`把权威持久Replay与最多1000条live-only文本Delta分离；溢出显式报告`liveGap`，客户端回退到完整Item；
- `ask_user`使用持久Question Request/Answer、`WAITING_INPUT`和配对Tool Result，进程重启后可继续回答，重复、冲突、取消与过期均有稳定语义；
- Steering绑定预期活动Turn并在模型步骤边界生效，不打断当前Provider请求，也不破坏模型响应、Tool Call、Tool Result和后续用户输入的历史顺序；
- 薄CLI只依赖Agent SDK，支持`create/list/run/follow/resume/retry/fork/archive/steer/cancel`，可显示计划、工具进度、Diff、审批、问题及流式回答。
- Skills从显式Bundled/User/Workspace Root生成不可变目录；普通名称仅在全局唯一时可用，正文和UTF-8资源按需复核摘要并通过只读`ExtensionActionPort`加载；
- Hooks只允许绑定宿主预注册的低风险只读Action；非Bundled定义在Registry捕获时需要精确摘要授权，`before_action`失败关闭，其他事件只记录，并具备Action执行超时、取消、哈希链和显式中断恢复；默认产品尚未接线。
- 严格JSON v2把Provider、模型Profile和Environment Secret引用分层，提供有界安全读取、v1原子迁移、不可变配置快照、离线能力诊断和活动配置CAS；配置及审计不保存Secret值；
- `SafeFallbackProvider`只在`transport/rate_limit/provider_internal`零响应暴露失败且审计成功后切换显式候选；任意响应、文本、Tool Call或未来未知事件均关闭Fallback窗口；
- `harnessix agent-server`按配置装配Provider、固定Workspace跨平台只读Coding Tools、Session绑定Artifact和stdio App Server；Provider构造或配置CAS失败时不开放协议，Windows不广告Git或写入能力。

0.8.4增加官方SDK驱动的MCP Client、不可变Tool目录、调用前Schema漂移检查、强Container stdio目标和可选低风险只读MCP Server；0.8.5增加不可执行Skill内容包、冲突消歧、跨平台安全渐进加载，以及精确授权的持久生命周期Hook；0.8.6完成模型Provider产品配置和内置stdio启动装配。所有准入Tool均由宿主Policy通过统一`ExtensionActionPort`进入Permission、Approval、Sandbox、审计和UNKNOWN恢复。远端MCP HTTP/OAuth、远端Skill安装、任意Shell Hook及公网Git认证仍未开放，分别进入0.9安全供应链和Dogfooding门禁。0.8仍不提供完整TUI、网络Agent Server或三平台安装器。MCP的当前目录、Schema、连接、UNKNOWN与可选Server边界见[MCP模块设计](docs/modules/mcp.md)；Skill的来源、目录、渐进加载、文件安全、访问账本和提示注入边界见[Skill模块设计](docs/modules/skills.md)；Hook的定义授权、匹配、状态、双账本、超时、取消和恢复边界见[Hook模块设计](docs/modules/hooks.md)；其他0.8设计与运行边界见[0.8详细设计](docs/m08-product-runtime-and-extensions.md)、[ADR 0075](docs/adr/0075-provider-profile-secret-and-safe-fallback.md)和[配置参考](docs/operations/configuration.md)。

## 当前已完成：代码可维护性与结构治理（0.9.0）

- 以固定AST/Tokenizer口径记录源码规模、模块说明、公共行为、高风险入口、复杂度、一级包依赖、依赖环和静态公共导出；
- 起始报告、最终报告和治理策略均采用版本化JSON，仓库路径及排序稳定，不导入生产模块，也不读取配置、数据库或Secret；
- 全部生产模块、公共行为以及25组状态机、副作用和恢复入口已具有邻近语义说明；Pydantic/Enum合同不因注释治理改变JSON Schema；
- `agent.reducer`保留稳定门面，Item投影、Turn投影和共用守卫按职责拆分，Session在线提交和离线Replay仍共享同一个`apply_event`；
- `make readability`和`make check`阻止新增说明债务、增长既有热点、新增未评审依赖边/依赖环及静态公共面漂移；
- 存量13个超大文件、148个符号热点和一个一级包强连通分量被精确冻结，不以机械拆分或模板化注释冒充治理完成。

研究事实、架构取舍、详细接口和验收边界见[源码研究](docs/research/code-readability-and-structure.md)、[ADR 0076](docs/adr/0076-code-readability-and-structural-governance.md)和[0.9.0详细设计](docs/m09-code-maintainability.md)。最终实现还关闭了Heartbeat与持久执行结果提交的竞态，并由[CI 34629640717](https://github.com/carrie1988/Harnessix/actions/runs/34629640717)完成六矩阵验收。

## 许可证与品牌

- 自包含[许可证决策ADR 0064](docs/adr/0064-agpl-and-commercial-dual-licensing.md)和新`LICENSE`的版本开始，社区版按照`AGPL-3.0-only`发布；
- 许可证切换前已经按MIT获得的历史版本继续适用其原MIT条款；
- 需要闭源嵌入、闭源修改或不按AGPL提供对应源码的主体，可申请独立[商业许可证](COMMERCIAL_LICENSE.md)；
- 代码许可证不授予Harnessix或Harnessix Code名称及Logo的品牌权利，详见[商标与品牌规则](TRADEMARKS.md)；
- 版权所有者、外部贡献和第三方依赖边界分别见[版权说明](COPYRIGHT.md)、[贡献指南](CONTRIBUTING.md)和[第三方通知](THIRD_PARTY_NOTICES.md)。

## 当前已实现：只读编码工具与事务 Artifact

- `CodingToolRuntime` 对接既有 Kernel，提供 `list_files` / `read_file` / `glob` / `grep`；
- 0.5.2b1 新增显式 Scoped 入口：Kernel 注入 Thread/Turn/Call 归属，旧接口继续兼容；
- 根身份、拒绝路径和工具契约绑定到持久版本/审批指纹；
- 目录 FD/no-follow、普通文件类型检查、链接拒绝与读取前后漂移检测；
- 严格 UTF-8、行/字节/扫描上限、分页 revision 与显式截断；
- 大小写敏感的路径通配与字面量搜索，固定忽略规则不放宽权限；扫描缺口显式计数；
- 显式开启 Artifact 后，搜索预览外记录以有界 JSONL 归档；正文、manifest 与 ToolResult 同一 SQLite 事务提交；
- `read_artifact` 按真实会话/工作区分页读取，具备 SHA 校验、配额、TTL、活跃会话保护和过期清理；
- 显式opt-in的连续只读有界并发、确定性结果提交、协作取消、线程与 FD 回收、SQLite 重开/Replay；
- 真实 SDK + 离线 HTTP 的搜索→revision 读取闭环，旧/Scoped 入口累计 18 个真实只读进程崩溃切点；不调用真实 API。

~~~bash
uv run python -m examples.kernel_files
uv run python -m examples.kernel_search
uv run python -m examples.kernel_artifacts
uv run pytest tests/tools tests/artifacts
~~~

上述 CodingToolRuntime 仅支持本地 macOS/Linux 只读范围，不是 OS Sandbox；仍不支持正则搜索、完整 gitignore、任意Shell或完整 Coding Eval。Git只在宿主显式提供固定可执行文件时注册；`run_tests`和模型 Patch 分别通过独立Process/Patch专用端口启用，不属于默认只读工具注册表。默认搜索仍只返回有界预览；Artifact 必须由宿主显式启用，单份最多 1 MiB/10000 记录，不是无限日志存储。详细输入输出、使用方式和下一阶段见 [0.5 实施设计](docs/m05-coding-tools.md)。

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

**范围边界**：这是宿主执行后端，不是模型可调用的写工具或完整编码 Eval；默认 Kernel 仍只读。副本最多 256 个文件、每文件 1 MiB、总计 32 MiB；不是完整 Git worktree，不运行钩子/代码，不自动合入源目录。私有目录和锁不等价于 OS Sandbox，也不能约束同 UID 的恶意进程。b2 已进一步交付下述宿主桥接 b2a 和 Kernel 写审批/模型工具 b2b。详见 [受管执行 ADR](docs/adr/0028-managed-patch-execution.md) 与 [实施设计](docs/m05-coding-tools-milestone-history.md#17-053b1-当前交付受管单文件执行)。

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
- 本节交付时为Agent v6/Session migration7；当前Agent Event为v20、Session migration23，可读取v1～v19历史事件；后续迁移只追加事实与交互能力，不静默改写历史事件。

~~~bash
uv run python -m examples.kernel_patch
uv run pytest tests/patches/test_kernel_patch*.py
~~~

范围仍为私有受管副本内的单文件精确编辑；不运行仓库代码、不合入源目录，不等于 OS Sandbox 或自主编码 Eval。接入、恢复和升级见 [ADR 0030](docs/adr/0030-kernel-managed-patch-admission.md)、[使用设计](docs/m05-coding-tools-milestone-history.md#20-053b2b-当前交付kernel-受管写闭环)。0.5.3c1 的只读整组准备与计划 Diff 见下节；c2a/c2b 已交付整组预留、审批、顺序消费与部分效果；c3a/c3b 已实现宿主桥接与 Kernel/模型批量闭环；c3c 再交付 Diff Artifact。

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

**边界**：仅宿主显式调用、仅私有受管副本内的已有普通文件。不承诺跨文件原子提交、内容 CAS 或自动回滚；不合入源目录，不运行 Shell。c2 本身不开放模型写工具；当前批量 Kernel 接入见 c3b，Diff Artifact 仍待 c3c。设计见 [ADR 0033](docs/adr/0033-batch-consumption-and-effect-recovery.md)，当时迁移见[部署里程碑历史](docs/deployment-milestone-history.md#副本账本-v3-升级053c2b)。

## 当前已实现：整组调用绑定与异步桥接（0.5.3c3a）

- 独立完整调用计划绑定 Thread/Turn/Call、工具/提案、副本和全部有序成员；旧单文件或后端批准不能替代调用批准；
- `ManagedPatchBatchBridge` 提供 prepare/review/execute/recover，复用现有组后端，不新增替换器或数据库格式；
- 取消、超时、排队和重复关闭均排空活动线程；公开效果与私有批准/运行证据分离；
- 重开必须核对原完整计划和宿主决定，缺少或损坏证据保持 unknown，不自动补跑。

~~~bash
uv run python -m examples.batch_patch_bridge
uv run pytest tests/patches/test_batch_bridge.py tests/patches/test_batch_bridge_cancel.py tests/patches/test_batch_bridge_crash.py
~~~

**边界**：这是受信宿主 API，本身不验证 Session 活跃性或持久消费；宿主必须使用原 Turn 截止时间和取消机制。当前通过下节 c3b 的专用端口接入 Kernel；默认仍不开放写工具，c3c 的 Diff Artifact 已通过独立发布端口接入（见下节）。详见 [ADR 0034](docs/adr/0034-batch-call-bridge-and-kernel-integration.md)。

## 当前已实现：Kernel 整组持久审批与写闭环（0.5.3c3b）

- 宿主显式注入 `AgentRuntime(..., patch_batches=ManagedPatchBatchBridge(copy))` 才广告/执行 `apply_patch_batch`；与原 `patches` 单文件端口可共存，不开放任意写注册；
- Session 保存完整调用计划、独立组审批与决定；持久离开等待后才镜像后端决定并一次性顺序执行。答复审批不会修改文件；
- 两个实际供应商 SDK 均通过离线 HTTP 完成“两文件读取→整组提案→审批重开→真实副本写入→逐文件读回”；没有新增真实模型调用；
- 私有 `ToolResult.patch_batch` 保留有界效果与运行原因，不进模型 wire，也不因公开结果超限丢失。部分效果停止当前 Turn；未知效果禁止自动继续；
- 本节交付时为Agent Event/Thread **v7**、Session **migration8**（当前Agent Event v20/Session migration23）；真实旧v6 wheel的只读/单文件审批升级通过，旧事件/投影原字节不重写，旧reader明确拒绝新库。副本账本保持 **v3**。

~~~bash
uv run python -m examples.kernel_batch
uv run pytest tests/patches/test_kernel_batch*.py tests/agent/test_batch_session_upgrade.py
~~~

这是已有普通文件的受管副本闭环，不是跨文件原子提交、源目录合入、OS Sandbox 或自主编码 Eval。取消等待或后端未镜像决定时，证明不足仍保守记为 unknown，不补批/重放。c3c1 报告准备与 c3c2 事务归档已交付，0.5.4a/b宿主进程与完整Agent/Process Saga及0.5.4c Git/测试反馈也已交付。详见 [设计](docs/m05-coding-tools-milestone-history.md#25-053c3b-当前交付kernel-整组持久审批与恢复)、[ADR 0035](docs/adr/0035-kernel-batch-approval-and-recovery.md) 和 [测试记录](docs/testing-and-evals-milestone-history.md#27-053c3b-kernel-整组闭环验收2026-09-04)。

## 当前已实现：真实计划/历史效果差异报告（0.5.3c3c1）

- `ManagedPatchBatchBridge.diff(...)` 绑定完整原调用、计划与真实组账本，生成未发布的宿主报告；效果视图还必须匹配原批准与已结算运行快照；
- 计划视图展示原提案；历史效果只为已归因成员展示编辑，未知和未执行成员保留独立说明，不冒充已经修改；
- JSONL 顺序为摘要、全部成员、编辑前缀；每记录24 KiB、整体最多1 MiB，默认64 KiB。预算不足以容纳全部成员时明确失败；文本与编辑截断不会隐藏；
- 只读取私有计划/历史证据，不读取当前目标、不追加观察、不补批、不重放；报告准备时未改变原工具定义、Agent v7、Session migration8 和副本v3；当前归档升级见下节。

~~~bash
uv run python -m examples.batch_diff
uv run pytest tests/patches/test_diff_document.py tests/patches/test_batch_diff_*.py
~~~

`complete` 只表示所选视图完整，不表示执行成功或已批准。原报告准备接口仍不写 Session；下节的独立发布器负责归档。

## 当前已实现：Diff Artifact 事务归档（0.5.3c3c2）

- 显式注入 `SQLiteBatchDiffPublisher`，计划引用与真实审批同事务，效果引用与真实 ToolResult/私有效果同事务；同一调用两用途互不覆盖。
- 失败、部分、未知效果不伪造成功；归档或预算失败可省略引用，不丢弃真实写效果。重开只核对，不重新执行；提交后丢确认不会重复归档。
- 复用分页、配额、TTL 和活跃会话保护；两个 SDK 的离线闭环可从效果引用继续调用 `read_artifact`。旧只读发布限制不变。
- 该片交付Agent **v8**/Session **migration9**；真实旧v7 wheel的三类审批、已有Artifact升级通过，旧Schema/事件原字节保留，旧reader拒绝新库。当前Writer已推进到Agent Event v20/Session migration23。

```bash
uv run python -m examples.batch_diff
uv run pytest tests/artifacts/test_batch_diff*.py
```

设计见 [ADR 0037](docs/adr/0037-batch-diff-transaction-publication.md)，当时部署见[部署里程碑历史](docs/deployment-milestone-history.md#当前-session-v8--migration9-升级053c3c2)。0.5.3c 范围已交付；Git/测试反馈、真实Coding Eval和受控单文件合入已由后续0.5.4c/0.5.5交付。任意Shell字符串被正式排除，非交互命令由受控`host.process`承担；OS Sandbox仍属于后续版本。

## 当前已实现：受信宿主进程运行层（0.5.4a）

- 固定可执行程序表、显式cwd/argv、环境允许列表、stdin EOF及额外FD关闭；不使用隐式Shell。
- stdout/stderr独立有界捕获与持续排水，Base64保留原始字节；严格区分截断、自然EOF和观察摘要。
- 超时、Token/Task取消、重复关闭、启动窗口、主进程提前退出及同组孙进程均有真实OS进程测试；TERM后必要时KILL，回收直接子进程。

```bash
uv run python -m examples.host_process
uv run pytest tests/processes
```

**边界**：这是受信宿主基础API，不是模型Shell工具或OS Sandbox。脱组后代、宿主硬崩溃和不可中断内核等待仍需后续设计；测试明确验证缺口并清理夹具。下节0.5.4b1已接Action Plane持久准入，b2已完成Agent绑定/当前范围恢复，0.5.4c已在同一链路上接入固定Git读取和测试Profile。设计见 [ADR 0038](docs/adr/0038-host-process-lifecycle.md)，Agent/Session格式及原工具定义不变。

### 持久命令准入（0.5.4b1）

宿主现在可以用`process_action_tool(factory)`将固定进程绑定显式注册到现有Action Plane。命令先持久化，必须提供幂等键并通过Policy/Approval，再进入租约执行；工具版本绑定cwd、程序身份、环境和资源预算。确定结果保存ProcessResult和Effect Receipt，证据不足则UNKNOWN。Task/宿主退出后不自动重放，也不根据历史PID杀进程。

该入口不在默认Bootstrap或模型工具清单中；命令argv会进入持久Journal，当前不支持SecretRef解析，不应承载凭据。Agent Session单一审批绑定和Process Artifact现已通过b2b/b2c2实现；0.5.4c已提供不接受模型argv的`run_tests`，宿主硬退出后的自动进程清理及OS Sandbox仍待后续实现。详见 [ADR 0039](docs/adr/0039-process-action-plane-admission.md)。

Agent接入的单一审批权威、跨库恢复Saga、WAITING_ACTION和Process Artifact边界已在 [ADR 0040](docs/adr/0040-agent-process-action-saga.md) 冻结。b2b1新增`AgentProcessCallPlan`并确定性绑定调用与Action身份；b2b2新增Agent Event/Thread v9、Session migration10、`ProcessApprovalRequestContent`、`ProcessActionStateContent`及`ToolResult.process`。Session决定只能从已核对的ActionSnapshot投影，Action Journal仍是唯一执行许可；READY/LEASED/RUNNING/RECONCILING保持持久WAITING_ACTION，只有终止观察可恢复工具循环。私有计划、批准与Action证据不会进入模型历史。

b2b2已用真实`e0e8498` v8 wheel完成跨安装升级、旧reader拒绝、旧事件/投影原字节保持以及migration10提交前后硬退出验收。b2c1 新增显式 `ProcessAgentBridge` / `ProcessRuntime`：模型调用只在宿主注入端口后可见，准备只提交稳定 Action，答复只写 Action 的唯一决定并立即进入 WAITING_ACTION，独立 `ActionWorker` 执行后由 `resume_turn` 单次读取并投影 READY/RUNNING/终态。相同决定重试和 Action 已决定但 Session 未投影的窗口只读原事实补齐；重复等待观察不追加事件。模型只取得不含 Base64 正文的流摘要。

b2c2 新增显式 `SQLiteProcessArtifactPublisher`。终态Action已捕获的stdout/stderr以`process-output/v1`规范JSONL保存：唯一摘要和二进制安全Base64分片，正文/manifest/Tool Result引用/终态Session事实同事务。读取复用`read_artifact`的Thread/工作区归属、分页、配额、TTL和清理，并额外核对Process批准、Action身份、双流摘要、分片偏移与正文哈希。单个Artifact无法容纳全部已捕获前缀时省略引用，不二次隐藏截断；归档失败不改变或重放Action。Agent Event仍为v9，Session migration11仅扩展Artifact用途白名单。详见 [ADR 0041](docs/adr/0041-process-output-artifact.md)。

b2c3 补齐完整恢复与取消：Runtime可从“ToolCall已提交但Session审批缺失”的窗口按稳定身份重取同一Action；八个跨库提交边界真实退出后不重复建Action或执行命令。WAITING_APPROVAL/WAITING_ACTION取消只停止Session等待并保守标记unknown，不撤销Action许可；RUNNING租约过期在SQLite/PostgreSQL中保存`UNKNOWN/lease_expired`结果供Agent投影。两个独立进程竞争审批时相同决定幂等、不同决定一胜一冲突。OpenAI与Anthropic实际SDK均通过离线HTTP完成审批重开、外部Worker、Artifact摘要读取和最终回答，私有Action证据与Base64正文不进入模型wire。详见 [ADR 0042](docs/adr/0042-process-saga-recovery-and-cancellation.md)。

```bash
uv run python -m examples.kernel_process
uv run pytest tests/agent/test_process_agent_runtime.py tests/agent/test_process_agent_crash.py
uv run pytest tests/agent/test_process_agent_sdk.py tests/artifacts/test_process_output*.py
```

默认 Agent 仍不暴露 `host.process`，桥接明确拒绝 `auto_execute=True`，审批答复不运行命令或无限轮询。Process Artifact和Session取消都不是执行许可撤销、OS Sandbox、DLP、孤儿进程监督或同UID防篡改边界。

## 当前已实现：Git与受控测试反馈（0.5.4c）

- `CodingToolRuntime(..., git_executable=<绝对路径>)`才注册`git_status`和`git_diff`；模型不能提交仓库路径、revision、pathspec、Git配置或任意子命令；
- Git固定禁用分页器、可选锁、Hook、fsmonitor、外部Diff和textconv，并要求工作区就是精确仓库根；状态最多200项，Diff返回最多48 KiB完整UTF-8前缀及已观察流摘要；
- `RunTestsAgentBridge`只向模型公开`{"profile": "unit"}`，宿主固定程序、argv、工作区和超时；完整命令仍写入原`host.process` Action，经过唯一审批和外部Worker；
- 测试非零退出是确定的执行结果，返回`passed=false`供模型继续修复；启动/清理或输出证据不完整仍按Process failed/unknown语义处理；
- 离线闭环已覆盖失败测试→读取Process Artifact→受管Patch审批→测试通过→Git状态/差异→最终回答；OpenAI与Anthropic官方SDK路径均不向模型wire泄漏固定argv和私有Action证据。

```bash
uv run python -m examples.coding_feedback
uv run pytest tests/tools/test_git.py tests/processes/test_test_profiles.py
uv run pytest tests/agent/test_coding_feedback_loop.py tests/agent/test_coding_feedback_sdk.py
```

这是受控测试Profile，不是任意Shell。测试代码仍在宿主权限下运行；当前没有容器/网络隔离、CPU/内存强配额或Git提交/推送。真实Provider多次Coding Eval与显式单文件工作树合入已由0.5.5交付。配置、数据、错误、恢复和取舍见 [ADR 0043](docs/adr/0043-git-and-controlled-test-feedback.md)。

## 当前已实现：Coding Eval评分与历史任务闭环（0.5.5a/b）

- `harnessix.coding-eval/v1`固定仓库来源、缺陷基线树、Prompt、允许路径、行为/回归检查、测试Profile和预算；
- 复用固定Git端口采集HEAD、变更类型、暂存区及完整Diff观察摘要，超过200项时拒绝不完整评分；
- 从真实Session顺序核对失败测试→已归因Patch→测试通过→Git状态/差异→结构化最终回答；
- 不比较唯一Golden Patch；区分`passed`、Agent失败`failed`和任务/基础设施无效`invalid`；
- 报告不保存Diff、测试日志、Prompt或模型summary，以最多1 MiB的0600原子JSON文件交付。
- 首个内置任务固定到Harnessix真实缺陷revision、tree OID和`ls-tree` SHA-256，只允许修改一个实现文件；
- 固定历史树被导出为不含remote和后续修复历史的私有单提交仓库，0700运行目录和0600 `ready`清单支持脏工作树重开；
- 宿主隐藏检查验证空工具调用ID兼容行为及四类身份回归，只向评分层提供退出码、耗时和输出摘要；
- 完整性错误不覆盖已有运行目录，检查取消和基础设施失败与普通行为失败明确分离。
- 运行器使用第二层受管执行副本，经同一Agent Runtime、Process/Patch持久审批和外部Action Worker形成失败测试、修复、通过测试、Git反馈和结构化回答闭环；
- `run-state.json`保存可恢复边界；Process审批后退出、调用取消和报告发布后退出均可按原Session/Action/Patch事实恢复，不重放已完成动作。

```bash
uv run pytest tests/evals
uv run python scripts/generate_specs.py
```

0.5.5b2的确定性脚本Provider只证明正式运行基础设施能够完成真实历史缺陷，不代表真实模型已经稳定完成该修复。真实Provider需要0.5.5c多次受控试验。设计见[ADR 0044](docs/adr/0044-coding-eval-contract-and-grader.md)、[ADR 0045](docs/adr/0045-historical-eval-materialization-and-checks.md)和[ADR 0046](docs/adr/0046-historical-eval-runtime-orchestration.md)。

## 当前已实现：多试验证据与受控执行基础设施（0.5.5c1/c2a）

- Campaign在首个请求前固定任务、源码revision、精确模型、有序run ID、价格快照和计费上下文；每次试验仍拥有独立工作区、Session和账本；
- 从原Turn逐尝试重算Token和成本，汇总`passed/provider/eval_infrastructure/runtime/task/budget`、端到端时延及成本完整性，未知Usage不填零；
- `harnessix coding-eval-campaign`默认禁网且不读配置，只有显式`--allow-network`才读取0600私有配置并创建Provider；
- Provider自动重试固定关闭，工具调用串行；0600非阻塞锁、持久完成前缀和报告摘要支持崩溃后只读核对或按固定run继续；
- 每个试验完成后重算累计已知费用；成本未知或试验间达到停止线时持久停止，不启动下一试验；
- CLI只输出白名单原因、计数、报告状态和已知金额，不回显配置、路径、供应商正文、响应ID或凭据。

```bash
uv run harnessix coding-eval-campaign --help
uv run pytest tests/evals/test_campaign*.py
```

费用停止线不是供应商账户硬额度：一个已开始试验可能越过停止线，达线只保证不再启动下一试验。c2b已使用百炼北京精确模型完成三次独立试验，累计费用估算¥0.273748；三次均在读取目标实现前触发任务20000累计Token预算，因此0/3不能解释为模型编码能力。完整结果和后续门禁见[真实基线记录](docs/validation/bailian-2026-09-06-coding-eval/README.md)，设计见[ADR 0047](docs/adr/0047-coding-eval-campaign-evidence.md)和[ADR 0048](docs/adr/0048-controlled-real-eval-campaign-execution.md)。

## 当前已实现：真实Eval预算版本化（0.5.5c3a）

- 固定源码研究区分当前上下文窗口、单响应输出限制和Turn累计消费预算，不改变Kernel已有预算语义；
- 历史任务v1及20000预算保持可复现，新增v2并把累计Token上限提升为100000；
- Catalog按任务ID与版本精确索引，无版本查询返回最新版本；旧Campaign按计划版本恢复，不会漂移到v2；
- Eval报告继续保存实际Token而非截断值，离线回归覆盖恰好到限与超过一个Token，以及v1/v2各自执行和只读重开。

c3a没有调用真实API。后续c3b已在新的独立Campaign中用相同精确模型完成三次任务v2试验；结果见下一节。研究与决策见[Token预算适用性研究](docs/research/eval-token-budget-applicability.md)和[ADR 0049](docs/adr/0049-versioned-eval-token-budget.md)。

## 当前真实证据：任务v2三次复测（0.5.5c3b）

- 固定提交`3975429`、任务v2、百炼北京`qwen3-coder-plus-2025-09-23`和三个独立run；41个模型步骤全部只有一次Provider尝试；
- 三次终态均为`budget`，合计324142输入、3717输出Token，完整已知估算费用¥1.35604；Provider、Eval基础设施和一般Runtime失败均为0；
- 三个模型都能执行初始测试并读取文件首页，但后续分页调用遗漏`expected_revision`；通用`tool_invalid_arguments`没有提供跨字段修正方法，模型连续重试并因完整历史增长耗尽100000累计Token；
- 三个工作区均无变更、无最终回答，因此0/3仍不是有效模型编码成功率。继续提高预算不能关闭该缺口，必须先交付有界、可操作且不回显参数值的工具校验反馈，再以新Campaign验证。

完整脱敏计划、报告、逐run指标和根因见[任务v2真实基线](docs/validation/bailian-2026-09-06-coding-eval-v2/README.md)。该Campaign已经完成，禁止修改或追加试验。

## 当前已实现：分页工具参数可纠正反馈（0.5.5c3c）

- `read_file(start_line > 1)`和`list_files(offset > 0)`缺少`expected_revision`时，在其他参数全部有效的前提下返回稳定`tool_expected_revision_required`；
- 消息只包含受信工具名和固定字段名，不回显路径、参数值、底层Pydantic错误或工作区内容；复合无效输入继续返回通用错误；
- 严格分页并发身份、输入/输出Schema、工具定义指纹和副作用边界均未放宽，不从历史隐藏注入revision；
- 错误作为普通Tool Result持久化并Replay，OpenAI-compatible与Anthropic实际SDK离线链路都能接收错误、提交新调用、复制前页revision并完成分页。

本片只证明纠正协议可运行，不证明真实模型稳定采用反馈。有效质量基线仍需0.5.5c3d在新授权、新Campaign和独立run中验证。设计见[参数校验反馈研究](docs/research/tool-validation-feedback-applicability.md)和[ADR 0050](docs/adr/0050-model-correctable-tool-validation.md)。

## 当前真实证据：分页纠正后三次复测（0.5.5c3d）

- 固定提交`7e58c15`、任务v2和三个独立run，38个模型步骤全部只有一次Provider尝试；
- 三个run均在一次`tool_expected_revision_required`后显式携带上一页revision并成功继续，真实纠正采用率3/3；
- 两个run完成允许文件修改、行为/回归检查、测试和Git核对，另一个因前期错误探索在Patch执行前超过100000累计Token；
- 严格结果为0/3：两个完成run的最终回答带Markdown围栏且字段结构错误。任务Prompt只写“按约定输出JSON”，当前模型请求未提供评分器要求的`summary/changed_paths/tests`契约，不能把该结果当作公平端到端成功率。

Campaign合计294662输入、4803输出Token，完整已知估算费用¥1.255496。完整脱敏证据见[分页纠正后真实基线](docs/validation/bailian-2026-09-06-coding-eval-v2-corrected/README.md)。既有Campaign保持不可变。

## 当前已实现：最终回答契约版本化（0.5.5c3e1）

- 保留任务v1/v2 Prompt和指纹，新增任务v3；
- v3继承v2全部仓库、检查、权限、步骤、时间和100000累计Token边界，只在模型可见Prompt中给出严格裸JSON结构并禁止Markdown围栏；
- 评分器保持严格，不剥离围栏、不猜字段，Git与测试机器事实仍是权威；
- Catalog默认返回最新v3，旧Campaign继续按计划中的精确版本恢复。

设计见[最终回答契约研究](docs/research/eval-final-answer-contract-applicability.md)和[ADR 0051](docs/adr/0051-versioned-eval-final-answer-contract.md)。独立v3 Campaign结果见下一节。

## 当前真实证据：任务v3三次严格通过（0.5.5c3e2）

- 固定提交`9d0be66`、任务v3与三个独立run，31次模型尝试全部为各步骤`index=1`；
- 三个run均采用`tool_expected_revision_required`完成分页纠正，只修改唯一允许文件，并通过行为、身份回归、测试反馈、Git反馈及裸JSON最终回答检查；
- 严格结果3/3，输入193539、输出3392 Token，P50 23.431778秒，完整已知估算费用¥0.828428；
- Provider、Eval基础设施、Runtime、任务和预算失败均为0，没有SDK自动重试或未知成本。

完整计划、聚合报告、单次指标和脱敏边界见[任务v3真实质量基线](docs/validation/bailian-2026-09-06-coding-eval-v3/README.md)。该结果只适用于固定历史任务，不外推为任意仓库成功率。

## 当前已实现：受控变更包与显式工作树合入（0.5.5d）

- `build_coding_eval_change_package`只接受`completed + passed`运行，重算状态、报告摘要、Git实况、受管Copy Manifest与前后完整镜像；当前只支持一个允许的已有UTF-8普通文件；
- `CodingEvalChangePackage`绑定任务、run、来源commit/tree、报告、路径、0644/0755权限、前后SHA-256和完整镜像；相同证据重复构建得到相同指纹，0600私有包不进入模型或脱敏报告；
- `CodingEvalDeliveryStore.prepare`要求目标origin、HEAD、tree OID、规范树摘要、Workspace scope和前镜像精确匹配，且staged、unstaged、untracked全部为空；
- 计划完整字段形成`approval_fingerprint`，只有匹配的显式`ApprovalRecord(APPROVED)`可执行；拒绝、错误指纹或脏工作区均不写目标；
- 执行采用同目录临时文件、文件`fsync`、持久临时inode意图、最终复核、原子替换与目录`fsync`，只修改工作树，不改index、不创建commit、不运行Hook；
- 重开通过前镜像、后镜像与临时inode区分安全重试、已应用、冲突和未知，不自动覆盖第三镜像或归因外部同内容写入。

真实历史通过run已生成稳定包，并在精确历史checkout上完成“脏工作区拒绝→显式批准→一次合入→幂等重开→Diff摘要一致→行为与身份隐藏检查通过”的离线验收。设计见[源码求证](docs/research/eval-change-delivery.md)与[ADR 0052](docs/adr/0052-controlled-eval-change-delivery.md)。该能力不是三方合并、自动commit/push、通用多文件发布或跨主机仓库锁。

## 当前已实现：Tool Contract与有界调度收口（0.5.6）

- `ToolDescriptor`/`ToolDefinition`新增默认关闭的`supports_parallel_calls`；只有只读工具可以声明，非法写能力在注册前失败；
- Kernel只并行同一响应开头连续、无需审批、契约匹配且显式opt-in的只读调用，默认上限4、范围1—16；
- 审批、Patch、Process、未知和未声明工具均为顺序屏障，屏障后的调用不能提前执行；
- 并发执行结果按Provider顺序持久化；任一异常立即取消并排空兄弟任务，Turn取消和Runtime关闭不遗留后台读取；
- `CodingToolRuntime`再以有界信号量限制文件、搜索、Git和Artifact读取，避免外部调用形成无界线程/FD占用；
- Patch、Process、Artifact、Test、Git和Workspace稳定错误统一归为`tool`类别，更具体的中断、审批、冲突和存储分类保持不变；
- 非交互命令由现有结构化`host.process`承担：宿主预绑定程序/cwd/env，模型提交argv并经过持久审批与Worker；不新增任意Shell字符串入口。

完整源码依据、失败语义、兼容和边界见[专项研究](docs/research/tool-scheduling-and-errors.md)与[ADR 0053](docs/adr/0053-tool-concurrency-and-error-taxonomy.md)。本次并发是单Runtime进程边界，不是跨进程Workspace锁或OS Sandbox。

## 当前已实现：Context规划、指令与检查记录（0.6.1）

- `harnessix.context`提供供应商中立`ContextFragment`、`ContextLimits`、`ContextPlanner`与`ContextInspection`契约；
- 固定Runtime > User > Project > Workspace > Git > Environment优先级，Runtime/User指令为必选，正文不能自报trust或priority；
- `utf8-bytes/v1`在模型调用前对历史、Tool Definition和指令分项估算，并预留输出、Provider开销和安全余量；
- 可选Fragment按稳定顺序装入，超限决策显式记录；历史/工具或必选指令超限时不发送Provider请求；
- OpenAI-compatible使用首个`system` message，Anthropic使用顶层`system`字段；
- 每个启用Planner的模型步骤先提交Agent Event v10 `ContextPrepared`，检查记录不复制指令正文；
- `AgentRuntime.inspect_context`、Context Span和低基数Token/Fragment指标提供持久诊断。

以上是0.6.1静态规划基线；0.6.2a和0.6.2b增加受控动态Source，0.6.2c增加Tool Result稳定模型视图，0.6.3增加自动Compaction与活动窗口，0.6.4和0.6.5完成Thread生命周期、终态Retry及Provider切换。精确Tokenizer仍属于后续优化。静态规划边界见[ADR 0054](docs/adr/0054-context-planning-and-inspection.md)，压缩边界见[ADR 0058](docs/adr/0058-compaction-windows-and-accounted-summary-attempts.md)。

## 当前已实现：受控项目指令Source与freshness（0.6.2a）

- `ContextSource`/`AsyncContextPlanner`把外部I/O与纯`ContextEngine`分离；同步静态入口保持兼容；
- `ProjectInstructionSource`绑定一个规范Workspace根，按根到工作目录逐层发现，同目录`AGENTS.override.md`优先于`AGENTS.md`；
- 读取复用Workspace no-follow、单硬链接、deny-path、分页revision、5秒截止时间和取消后线程回收；默认总量64 KiB，超限失败而非截断；
- 未发现和空白形成`empty`，非法文件、超限、暂时不可用和Workspace错配具有独立稳定错误；所有失败均发生在Provider请求前；
- `ContextInspection v2`持久source/document revision、scope、字节数和Fragment绑定但不复制正文；Agent Event/Thread升级至v11，Session migration 13不改写旧事实；
- `harnessix.agent.context.sources`只输出固定kind/status标签，不输出路径、正文、scope或revision。

该切片只完成Project Instruction Source；0.6.2b已在下一节补齐Workspace/Git/环境Source和跨来源一致性。完整源码依据、失败语义和部署边界见[专项研究](docs/research/context-sources-and-tool-results.md)、[ADR 0055](docs/adr/0055-project-instruction-source-and-freshness.md)及[0.6实施设计](docs/m06-context-and-sessions.md)。

## 当前已实现：Workspace/Git/环境Source与跨来源一致性（0.6.2b）

- `WorkspaceContextSource`通过既有Workspace与`list_files`列出根和工作目录的有界一级概览，不递归读取代码正文；deny-path、no-follow、revision、扫描上限和取消边界保持有效；
- `GitContextSource`只复用固定`GitReadRuntime.status`，提供仓库标志、分支/HEAD/upstream/ahead/behind和有界脏状态；远端URL、Git用户、日志和配置不进入Context，deny-path状态项不会泄漏；
- `EnvironmentContextSource`只按宿主显式allowlist逐项取值，不枚举进程环境；Secret类名称、控制字符、异常类型和字节越界在发网前拒绝；
- 单Source保持Context Inspection v2的一次观测语义；两个及以上Source执行固定两轮顺序观测，scope或revision漂移失败关闭，同revision正文漂移视为来源契约错误；
- 多Source使用`ContextConsistencySnapshot v1`与Context Inspection v3；Agent Event/Thread升级至v12，Session migration 14不改写历史事实；
- Context一致性只证明两轮有界窗口内未检测到变化，不是文件系统/Git事务，也不替代工具revision、Policy、Approval和效果核对；
- Workspace默认每目录64项/12 KiB，Git默认100项/16 KiB，环境最多32个allowlist键/4 KiB，所有截断均显式记录。

0.6.2c的Tool Result模型视图见下一节；动态Source本身不改写Session原始Item。设计与安全边界见[ADR 0056](docs/adr/0056-workspace-git-environment-sources-and-consistency.md)和[0.6实施设计](docs/m06-context-and-sessions.md)。

## 历史基础：0.1 Action Plane兼容内核

0.1曾交付framework-agnostic Action Contract、Policy、Approval、Effect Journal、Worker Lease、`UNKNOWN`和
Reconcile，并由此形成当前可信执行语义。产品演进后，默认Coding Agent已经使用
`TrustedActionGateway → TrustedActionRouter`统一规划、审批、执行和对账。

从[ADR 0081](docs/adr/0081-single-coding-agent-product-boundary.md)开始，独立HTTP API、Action HTTP Client和
数据库Worker Queue不再属于1.0产品面；固定Container Process和Git Push已经迁入Trusted Action链，`ActionService/ActionWorker`
只为历史Process Reader、Eval和旧实现迁移保留，不允许新增调用方。旧实现的历史能力和测试证据仍保留在Git历史与
[Action Plane子系统资料](docs/subsystems/action-plane.md)中，不能据此使用已撤销的`serve/worker`命令。

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
- 版本化Agent Event、Session历史迁移，旧事件不改写（当前v19；既有v1–v16回归及多代旧包升级证据保留）；
- SessionStore 共享契约和损坏/不可写/磁盘满等故障测试；
- Transcript Replay、投影重建和真实进程故障注入。

离线验收：

~~~bash
uv run pytest tests/agent
uv run python examples/kernel_replay.py
uv run python examples/kernel_approval.py
uv run --extra observability python -m examples.kernel_observability
~~~

Plan/Compaction语义Item支持可信宿主记录与Replay。0.6.3新增[窗口规划](docs/compaction-window-planning.md)、[独立摘要账本](docs/compaction-attempt-ledger.md)和[自动Compaction运行时与活动窗口](docs/compaction-runtime-and-windows.md)。摘要请求在HTTP前持久化意图，候选与窗口分离提交，重复压缩不恢复旧原始前缀。

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
- 当时交付Agent Event/Thread v4、Provider Event v2、真实v1/v2/v3会话升级与冻结Schema（当前为Agent Event v19/Provider v3）；
- 两类实际 SDK 在 HTTP 前发布尝试意图，重试使用独立 UUID，不把意图当作已收费；
- 缓存读取/创建与公开推理计数映射、响应失败时保留最后合法观测；
- 当时交付 23 个模型尝试相关子进程崩溃切点，全项目合计 49 个；0.4.3b2 后分别为 28 / 54 个；差额 Token 指标。

两个SDK均使用Provider v3尝试元数据（兼容原v2尝试语义）；旧自定义Provider的响应记账路径保持兼容。`Turn.usage`只是已知消费下界，需同时查看`usage_is_complete`，缺失分项仍为`null`；它不是成本或供应商账单。价格估算与受控真实验证证据见后续对应章节。设计见[ADR 0016](docs/adr/0016-model-attempt-ledger.md)与[ADR 0017](docs/adr/0017-provider-attempt-usage.md)。

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

操作说明、退出码、凭据引用和隐私边界见[Smoke使用说明](docs/model-smoke.md)，配置、场景、预算、恢复、安全与源码测试映射见[Smoke模块设计](docs/modules/smoke.md)。Token检查不等于金额硬上限；响应计费元数据已在0.4.3b2接入，但不自动识别平台计价规则。

## 当前已实现：0.4.3b2 响应计费元数据

- 服务等级、推理地域、5m/1h 缓存写入分项与 Usage 原子持久化；缺失保持未知，重复不重计、漂移拒绝；
- OpenAI/Anthropic SDK 映射、失败/取消/崩溃保留与 Replay；
- Agent Event/Thread v5、Provider Event v3，真实 v1–v4 混合升级，旧 Schema 不改写；
- 仅对宿主明确声明的匹配直连平台映射计价上下文；代理/百炼不自动套用原生价格；
- 混合/不完整 TTL 不强行选单一费率，价格绑定拒绝与已观测事实冲突；CostReport v1 保持重算兼容。

设计见 [ADR 0020](docs/adr/0020-observed-billing-context.md)。0.4 整体验收和真实编码工具仍未完成。

## 当前产品快速开始

环境要求：Python 3.12+、[uv](https://docs.astral.sh/uv/)以及一个可用的OpenAI-compatible或Anthropic模型配置。

```bash
make install
make check

uv run harnessix code configure \
  --provider-kind openai_chat \
  --base-url https://example.invalid/v1 \
  --model example-model \
  --non-interactive

uv run harnessix code doctor /path/to/workspace
uv run harnessix code /path/to/workspace
```

需要启用固定Container Process Profile时，显式提供独立Action配置；Doctor与正式启动读取同一文件：

```bash
uv run harnessix code doctor /path/to/workspace \
  --action-config "$HOME/.harnessix/actions.json" --json
uv run harnessix code /path/to/workspace \
  --action-config "$HOME/.harnessix/actions.json"
```

外部Action配置必须满足严格v1合同和私有文件要求。切换已激活配置时还必须携带上一活动摘要
`--expected-active-action-sha256`，否则启动在开放stdio前以CAS冲突失败关闭。

`harnessix code`启动本地TUI并监督stdio Agent Server；`harnessix agent-server`是产品内部Headless入口，
其stdout只传输Agent Protocol JSONL。Python宿主使用`harnessix.sdk.AgentClient`及进程内或子进程Transport。

独立`harnessix serve`、`harnessix worker`、Action HTTP SDK和LangGraph Action Adapter已经退出1.0产品面。
其他Agent框架若需要接入，应使用版本化Agent Protocol；未来远程执行只会作为Trusted Action Executor后的
受信适配器立项，不会恢复绕过Thread/Turn的第二套公共Action API。详细迁移边界见
[0.9.1f设计](docs/changes/m09-1f-single-product-runtime-convergence.md)。

## 当前仓库结构

```text
src/harnessix/domain/       旧Action合同（迁移兼容）
src/harnessix/storage/      旧SQLite/PostgreSQL Journal（迁移兼容）
src/harnessix/policy/       旧Action Policy（迁移兼容）
src/harnessix/executors/    旧Action样例Executor（迁移兼容）
src/harnessix/sdk/          Agent Protocol Python客户端与Transport
src/harnessix/trusted_actions/ 统一高风险Action规划、审批、执行与对账
src/harnessix/product_config/  产品配置、能力探测与默认组合根
src/harnessix/agent/        Kernel领域模型、Reducer稳定门面及Item/Turn投影、Loop、取消
src/harnessix/models/       Provider 契约、Fake/Scripted Provider
src/harnessix/session/      SQLite Session Store、迁移与宿主锁
src/harnessix/tools/        工作区只读/Git工具、作用域与Artifact读取入口
src/harnessix/artifacts/    有界正文、事务发布、分页、配额与清理
src/harnessix/patches/      受管单文件/整组Patch及差异报告
src/harnessix/processes/    进程监督、旧Action桥接、测试Profile与输出文档
src/harnessix/evals/        版本化编码任务、运行、评分、Campaign与受控交付
src/harnessix/protocol/     Agent Protocol合同、Codec、Replay与请求账本
src/harnessix/app_server/   stdio Headless服务与应用编排
src/harnessix/mcp/          MCP目录、连接、可信Action适配与只读Server
src/harnessix/skills/       Skill目录、冲突、渐进读取与Action适配
src/harnessix/hooks/        Hook定义/授权、生命周期运行、持久化与恢复
src/harnessix/product_config/ Provider/Profile配置、迁移、诊断、审计与产品装配
tests/                      单元和集成测试
docs/                       中文架构与决策文档
spec/                       生成的 JSON Schema 和 OpenAPI
examples/                   可运行演示
```

后续仍按里程碑增量加入新模块，不进行一次性目录重写。

## 设计资料

- [文档中心：当前事实、历史决策、研究与证据的统一入口](docs/README.md)
- [源码阅读地图：产品启动、Agent Loop和可信执行](docs/guides/source-reading-map.md)
- [产品章程](docs/product-charter.md)
- [总体架构](docs/architecture.md)
- [1.0本地优先商用边界决策](docs/adr/0062-local-first-v1-commercial-boundary.md)
- [Windows 1.0平台支持决策](docs/adr/0063-windows-v1-platform-support.md)
- [AGPL与商业双许可决策](docs/adr/0064-agpl-and-commercial-dual-licensing.md)
- [主流 Coding Agent 源码研究计划](docs/research-plan.md)
- [0.2 源码研究基线](docs/research/baselines.md)
- [Agent Loop 研究](docs/research/agent-loop.md)
- [Session 模型研究](docs/research/session-model.md)
- [协议与 Provider Event 研究](docs/research/protocol.md)
- [Tool Runtime 研究](docs/research/tool-runtime.md)
- [Tool调度与错误分类专项研究](docs/research/tool-scheduling-and-errors.md)
- [Context Engine 研究](docs/research/context-engine.md)
- [Context规划、指令与预算源码研究](docs/research/context-planning-and-instructions.md)
- [Permission、Approval 与 Sandbox 研究](docs/research/security.md)
- [Coding Agent多试验质量与成本研究](docs/research/eval-campaign.md)
- [Coding Eval Token预算适用性研究](docs/research/eval-token-budget-applicability.md)
- [Coding Tool参数校验反馈适用性研究](docs/research/tool-validation-feedback-applicability.md)
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
- [0.6 Context Engine与持久会话实施设计](docs/m06-context-and-sessions.md)
- [Thread Resume、Fork与Archive详细设计](docs/thread-lifecycle.md)
- [Turn Retry与Provider切换详细设计](docs/turn-retry-and-provider-switch.md)
- [终态Turn Retry与Provider中立历史决策](docs/adr/0061-terminal-turn-retry-and-provider-neutral-history.md)
- [Provider/Profile配置与安全Fallback源码研究](docs/research/provider-profile-config-and-safe-fallback.md)
- [Provider/Profile、Secret引用与安全Fallback决策](docs/adr/0075-provider-profile-secret-and-safe-fallback.md)
- [代码可读性与结构治理源码研究](docs/research/code-readability-and-structure.md)
- [代码可读性、可维护性与结构治理决策](docs/adr/0076-code-readability-and-structural-governance.md)
- [0.9.0代码可维护性详细设计](docs/m09-code-maintainability.md)
- [进程内宿主与初始投影决策](docs/adr/0011-kernel-host-and-initial-projection.md)
- [Action Contract](docs/action-contract.md)
- [Action 生命周期](docs/action-lifecycle.md)
- [自研与复用边界](docs/build-vs-buy.md)
- [设计与开发路线图](docs/roadmap.md)
- [M1 Worker 与 PostgreSQL 设计](docs/m1-worker-postgresql.md)
- [M1.2 可观测性设计](docs/m1-observability.md)
- [部署与运维](docs/deployment.md)：当前拓扑及安装、配置、升级、恢复、诊断与平台入口
- [Process输出Artifact决策](docs/adr/0041-process-output-artifact.md)
- [Git与受控测试反馈决策](docs/adr/0043-git-and-controlled-test-feedback.md)
- [Coding Eval多试验证据决策](docs/adr/0047-coding-eval-campaign-evidence.md)
- [受控真实Coding Eval Campaign执行决策](docs/adr/0048-controlled-real-eval-campaign-execution.md)
- [版本化Coding Eval Token预算决策](docs/adr/0049-versioned-eval-token-budget.md)
- [模型可纠正工具参数校验反馈决策](docs/adr/0050-model-correctable-tool-validation.md)
- [受控Eval变更交付决策](docs/adr/0052-controlled-eval-change-delivery.md)
- [Tool有界并发与错误分类决策](docs/adr/0053-tool-concurrency-and-error-taxonomy.md)
- [Context规划、指令优先级与检查记录决策](docs/adr/0054-context-planning-and-inspection.md)
- [百炼北京三次Coding Eval基线](docs/validation/bailian-2026-09-06-coding-eval/README.md)
- [百炼北京任务v2三次Coding Eval基线](docs/validation/bailian-2026-09-06-coding-eval-v2/README.md)

## 目标里程碑

| 版本 | 结果 |
|---|---|
| 0.2 | 产品、源码研究与架构基线 |
| 0.3 | 可恢复 Agent Runtime Kernel |
| 0.4 | OpenAI-compatible / Anthropic Model Runtime |
| 0.5 | Read/Search/Patch/Process/Git/Test 编码闭环 |
| 0.6 | Context Compaction 与持久会话 |
| 0.7 | 跨平台端口、可信执行、通用Process、多文件事务与Git交付 |
| 0.8 | Agent Protocol、Headless、薄CLI、MCP、Skills、Hooks、Provider/Profile产品配置 |
| 0.9 | 代码可维护性治理、完整CLI/TUI、三平台CI与发行物、故障注入、质量工程和Dogfooding |
| 1.0 | macOS/Linux/Windows本地优先正式商用发布 |
| 1.x | 按需求评估云任务、多租户、IDE与分布式运行 |

## 重要语义

Harnessix 不承诺任意外部系统上的神奇 Exactly Once。它提供的是：

> Action 身份稳定、可幂等时安全复用、结果不确定时停止盲目重试，并通过外部观察和对账尽量实现业务级 Effectively Once。

## 当前已实现：Tool Result稳定模型视图（0.6.2c）

- Session原始结果保持不变，Context预算和Provider使用同一份深拷贝视图；
- 每个结果默认64 KiB。首次进入模型历史时冻结策略、规范JSON摘要和精确替换，后续步骤复用；
- 超限Grep/Glob只省略已经由完整Artifact覆盖的记录列表，查询、统计和完整性信息不丢失；其他结果不做任意JSON截断；
- 模型调用前检查当前工作区权限、Thread/Call归属、引用用途、TTL、manifest、正文摘要和省略覆盖；分页回读另核对原发布者与消费调用；
- 自Event v13起每步记录`ModelHistoryPrepared`；取消、验证超时、失效引用或提交失败不会调用下一步模型；
- 旧会话没有冻结证据时只允许原样inline，不因升级而改变已经进入模型的前缀。

宿主可通过`AgentRuntime(tool_result_view_policy=ToolResultViewPolicy(max_inline_utf8_bytes=65536))`配置单结果预算。`ToolResultViewPolicy`从`harnessix.context`导入；归档验证默认复用绑定同一Session的SQLite发布器，访问能力来自Coding Tool Runtime或原Batch Diff桥接。无归档且超限时明确失败，不自动补写旧结果或重试工具。

正式接口、失败代码、数据版本和恢复语义见[ADR 0057](docs/adr/0057-tool-result-model-view-and-artifact-binding.md)、[实施设计](docs/m06-context-and-sessions.md#29-062c实现边界)和[部署规范](docs/deployment.md)。本能力不等于Compaction，也不代表整体生产商用版本完成。

## 当前已实现：Thread生命周期（0.6.4）

- `resume_thread`只读返回同一Thread，不调用Provider或Tool；
- `fork_thread`在终结Turn边界冻结有界模型历史，继承事实固定`authority=none`，不继承审批或执行权；
- Fork保留Artifact真实父级或祖先所有者，并以当前Workspace能力在发网前重新验证；
- `archive_thread`在无活跃Turn时将Thread原子转为不可变只读状态；
- 来源CAS、确定性子身份、七个进程退出切点及v15→v16 wheel升级已经验收；
- 实现提交通过[CI 34188329001](https://github.com/carrie1988/Harnessix/actions/runs/34188329001)四矩阵。

设计见[ADR 0060](docs/adr/0060-thread-lifecycle-and-authority-free-forks.md)与[Thread生命周期详设](docs/thread-lifecycle.md)。

## 当前已实现：终态Turn Retry与Provider切换（0.6.5）

- `retry_turn`只从最新`failed/cancelled/interrupted`来源创建新Turn，来源终态永不重开；
- 新Turn持久记录`retry_of_turn_id`并使用固定续作输入，普通Turn请求指纹保持兼容；
- 来源存在`ToolResult.outcome=unknown`时失败关闭，Retry不能代替外部效果对账；
- OpenAI-compatible与Anthropic双向切换时，由规范Item重建目标协议，历史原生Tool Call ID和模型绑定metadata不跨Provider发送；
- 三个接受事务硬退出窗口、取消/中断续作、双向真实Adapter和压缩→恢复→重试→Fork→Archive长会话已经本地验收；
- 当前Agent Event为v20，Session migration23；既有v16→v17独立wheel升级保持旧字节，旧reader失败关闭，migration20～22依次追加协议请求账本、deferred Turn和持久交互能力，migration23追加Trusted Action v20投影语义标记。

源码依据、决策和接口见[专项研究](docs/research/turn-retry-and-provider-switch.md)、[ADR 0061](docs/adr/0061-terminal-turn-retry-and-provider-neutral-history.md)与[详细设计](docs/turn-retry-and-provider-switch.md)。实现提交已通过[CI 34192389373](https://github.com/carrie1988/Harnessix/actions/runs/34192389373)四矩阵验收，0.6正式关闭；0.7及后续生产能力仍按路线图推进。
