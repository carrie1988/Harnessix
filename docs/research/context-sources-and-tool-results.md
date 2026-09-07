# Context Source、项目指令与 Tool Result 模型视图源码研究

- 更新日期：2026-09-08
- 适用范围：Harnessix Code 0.6.2
- 研究状态：0.6.2a、0.6.2b已实现；0.6.2c Tool Result模型视图源码研究与架构决策已冻结

## 1. 研究问题

0.6.1只接受宿主显式提供的静态 Fragment。0.6.2需要回答：

1. 项目指令从哪里发现、按什么顺序生效、怎样限制目录范围；
2. 文件不存在、空白、读取失败、读取时变化分别代表什么；
3. 动态来源怎样在每次模型步骤刷新，并留下不含正文的持久 freshness 证据；
4. 来源 I/O 怎样响应 Turn 取消、超时并回收文件描述符和线程；
5. Tool Result 怎样限制模型视图而不破坏完整历史、恢复和 Artifact 可取回性。

## 2. 固定源码基线

| 项目 | 本地源码提交 | 研究性质 |
|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | 主要实现证据 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | 主要实现证据 |
| Claude Code 逆向整理源码镜像 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | 非官方材料，仅作交叉验证，不作为单一契约依据 |

结论绑定上述提交和具体文件，不把项目名称、README描述或当前产品行为替代为源码事实。

## 3. 项目指令发现

### 3.1 Codex

`codex-rs/core/src/agents_md.rs:1-16`明确从项目根到当前工作目录收集指令且不越过项目根；`39-46`定义`AGENTS.override.md`优先于`AGENTS.md`；`115-183`区分“未发现”和异常 I/O，并记录来源；`185-280`实现根到当前目录的候选发现和同目录候选优先级。

可借鉴点：

- 作用域是项目根到当前目录，不是递归扫描整个仓库；
- 每层最多选择一个候选文件，较深目录规则随后出现；
- 文件来源必须保留，读取失败不能伪装成未配置；
- 总读取量必须有界。

不可直接照搬点：该实现允许符号链接且超限时截断。Harnessix已有更严格的Workspace no-follow、单链接文件和分页revision契约；项目指令属于控制模型行为的高影响输入，静默截断可能改变规则含义，因此选择拒绝超限。

### 3.2 OpenCode

`packages/core/src/instruction-context.ts:40-88`在当前目录到项目根间发现`AGENTS.md`，对项目文件消失返回 unavailable，对完全没有文件返回 empty；`99-100`把路径与正文一起渲染。

`packages/core/src/system-context/index.ts:5-17`把系统上下文建模为可独立刷新的来源；`48-57`定义持久快照；`197-215`在首次观测存在 unavailable 时阻止初始化；`217-290`区分更新、删除和暂时不可用，已有快照不会因暂时观测失败被误删。

可借鉴点：

- empty 与 unavailable 是不同状态；
- 来源有稳定命名空间身份和可比较快照；
- 首次来源不可用应阻止生成不完整上下文；
- 更新决策需要持久化，Resume 后不能依赖进程内缓存猜测。

Harnessix 0.6.2a尚未持久化来源正文，因而没有足够证据在刷新失败时重用旧正文。当前选择每个模型步骤重新读取，任何 unavailable 均发网前失败；后续如需 stale fallback，必须先增加加密/受限正文存储、有效期和明确的 stale 状态，不能只凭旧摘要恢复。

### 3.3 Claude Code 逆向整理源码镜像

`src/context.ts:36-111`显示Git状态使用有界、缓存的启动快照；`113-189`显示系统/用户上下文按会话缓存并包含项目说明文件。它说明“来源时效”必须是显式产品选择：启动快照、每步刷新和按需刷新具有不同一致性含义，不能都叫“当前状态”。

该材料不具备官方源码的权威性，因此只用于验证问题存在，不决定Harnessix契约。

## 4. Tool Result 模型视图

### 4.1 Codex

`codex-rs/utils/output-truncation/src/lib.rs:14-31`提供带原始规模提示的中间截断；`35-49`按文本或内容块分别处理函数结果；`52-107`合并文本预算但保留图片、音频和加密内容的结构与顺序；`109-178`按一个总预算逐项处理内容块，并对省略的文本、音频显式生成计数标记。`codex-rs/core/src/context_manager/history.rs:224-280`在历史入栈时复制Response Item，再对函数结果施加模型配置预算；原调用对象不被就地修改，后续`for_prompt`另做模态规范化。

该实现证明Tool Result必须在进入模型历史前有界，且不同模态不能统一按字符串处理。它没有提供Harnessix所需的完整结果Artifact、跨重启替换决策和引用归属校验，因此不能直接作为恢复契约。

### 4.2 OpenCode

`packages/core/src/session/compaction.ts:12-15`设定摘要缓冲、保留量、Tool输出和摘要输出上限；`83-120`只在构造Compaction输入时把工具/命令输出裁至2000字符。`packages/core/src/session/runner/to-llm-message.ts:26-62`从持久Tool状态构造Provider结果，`64-107`生成新的LLM消息数组；两条路径都没有反向改写Session消息。Tool输出在Compaction输入中的简化与正常模型历史投影是两个不同阶段。

该分离可避免摘要预算策略污染事实历史，但OpenCode此处的`slice(0, 2000)`只适用于面向摘要器的文本序列化，不足以定义结构化JSON、媒体或可取回完整证据的生产契约。

### 4.3 Claude Code 逆向整理源码镜像

`src/utils/toolResultStorage.ts:137-183`使用Tool Use ID确定文件名并以排他创建保存完整正文，已存在时复用；`189-199`构造预览和文件引用。`272-334`先判断类型和规模，完整持久化成功后才替换模型结果，图片块保持原结构。`367-412`维护每会话冻结状态并为共享缓存的分支复制；`465-479`把模型实际看到的替换字符串写入Transcript，而不是恢复时按新代码重新生成。`641-667`区分必须重放、已见未替换和首次出现的结果；`694-725`构造新消息而不修改原消息；`739-908`只选择首次出现的大结果，持久失败则保留原文并冻结该决定；`938-987`从Transcript恢复替换状态。

该镜像揭示了Prompt Cache前缀稳定性的实际约束：同一结果第一次进入模型后的命运不可在后续轮次改变。其本地绝对路径引用、持久失败后继续发送可能超限原文和基于字符的规模估算不适合Harnessix；材料仍只作交叉验证。

### 4.4 Harnessix 0.6.2c约束

Tool Result切片必须同时满足：

- Session原始完成Item不可被模型视图裁剪反向改写；
- 只有已成功提交的完整Artifact才能生成“预览+引用”；
- 引用必须绑定Thread、Workspace scope、Artifact ID、完整性和过期语义；
- 相同Item在Resume/Fork和重复规划时得到相同模型视图；
- Artifact发布失败不得静默丢弃完整结果后继续调用模型；
- 文本、结构化JSON、媒体、Patch/Process专用证据需分别定义，不能用字符串切片覆盖所有类型。

### 4.5 结论与取舍

1. Session中完成的`Item`是事实源。模型历史只由其深拷贝构造，任何预算处理都不得追加`ItemFinished`覆盖旧结果，也不得更新旧事件。
2. 以Provider可见的`outcome/output/error/diff_artifact`规范JSON UTF-8字节数作为单结果边界；默认上限64 KiB。字符数、Token估算和Session接收上限不能替代该边界。
3. 结果首次进入模型历史时冻结`inline`或`artifact_reference`决定。决定记录来源Item/Call、来源和视图摘要、字节数、当时上限、Artifact绑定及精确替换`output`。恢复和后续步骤复用已记录决定；更小的新上限若容不下旧视图则失败，不允许重写历史前缀。
4. 未超限结果保持完整结构。超限结果不做头部、尾部或中间字符串切片；只有`output={preview, artifact}`且Artifact用途为`tool_result`、`complete=true`时，才把整个preview替换为固定省略元数据并保留原引用。Artifact正文必须已与结果同事务提交。
5. 每次模型步骤在`PREPARING_CONTEXT`阶段先校验全部可见Artifact，再持久化`ModelHistoryPrepared`。检查记录只保存计数、字节数和摘要；替换决定只保存有界替换元数据，不复制工具正文。
6. Process输出Artifact只证明已捕获stdout/stderr文档，Batch Diff只证明计划或效果差异；二者都不能授权删除任意Tool Result字段。Patch/Process结果若异常超过边界则失败关闭。
7. 当前`ToolResultContent.output`仅为JSON值，不具备媒体类型、MIME、尺寸和Blob引用契约。媒体不得伪装成大字符串后由通用逻辑裁剪；正式媒体结果留待专用契约。
8. 任何模型可见Artifact引用在发网前校验Thread、Call、用途、Session关联、manifest、状态、TTL、正文长度、SHA-256和记录数。缺少验证器、跨归属、过期、损坏或不完整引用均不调用Provider。
9. 0.6.2c采用单结果边界，不追溯改变已见结果以满足后来增长的聚合预算。历史总预算继续由Context Engine拒绝，并在0.6.3通过版本化Compaction解决。

## 5. 0.6.2a 决策结论

1. 新增异步`ContextSource`端口和`SourcedContextEngine`，静态`ContextEngine`继续保持纯计算兼容入口；
2. Source只返回文档、revision和workspace scope，Fragment kind由宿主绑定，动态来源不能声明Runtime/User信任级别；
3. `ProjectInstructionSource`绑定一个规范Workspace根，只查根到配置工作目录的祖先链；同目录按`AGENTS.override.md`、`AGENTS.md`选择一个；
4. 所有读取复用Workspace no-follow、单链接、deny-path、分页revision和5秒读取截止时间；总正文默认64 KiB，超限拒绝，不截断；
5. 不存在和仅空白文件形成`empty`快照；非法UTF-8、二进制、链接、错类型和越界形成`context_source_invalid`或`context_source_too_large`；竞态、I/O和超时形成`context_source_unavailable`；
6. 每次模型步骤刷新，先形成`ContextInspection v2`无正文来源快照并以Agent Event/Thread v11持久化，再调用Provider；
7. 取消会停止ReadOperation并等待线程释放FD，不留下后台读取；
8. Source正文、文件revision、workspace scope和source identity均不进入指标标签。

## 6. 未纳入本切片

- Workspace、Git和环境的正式Source实现与组合一致性；
- 项目根自动探测、配置化fallback文件名、全局用户指令文件；
- 针对被编辑文件所在子目录的按Tool目标规则再发现；
- 暂时不可用时的持久旧正文回退；
- Compaction及超限结果的通用自动归档。

这些能力分别进入后续0.6.3及更晚切片，未实现前不得描述为生产完成。

## 7. 0.6.2b Workspace、Git与环境上下文研究

### 7.1 Codex

`codex-rs/core/src/context/environment_context.rs:32-70`从已生效的Workspace根和文件权限配置构造文件系统上下文，`193-208`对动态文本执行结构转义。`codex-rs/core/src/context/world_state/environment.rs:27-69`只组合明确的环境状态、日期、时区、网络和文件系统能力；`102-190`持久比较快照并只渲染变化；`315-383`把工作目录、状态和Shell建模为结构化字段。该实现说明模型需要的是宿主选定的环境事实和权限视图，而不是整个进程环境变量表。

`codex-rs/git-utils/src/info.rs:39-41`为Git命令设置五秒超时并关闭Hook路径；`62-118`先判断仓库，再并行读取提交、分支和经清洗的远端，非仓库或失败返回空值。`codex-rs/core/src/git_info_tests.rs:343-466`覆盖非Git目录、普通仓库、远端、detached HEAD和分支。该模块主要提供应用元数据，不能直接证明其全部字段进入模型上下文；可借鉴的是有界执行和非仓库显式语义。

Harnessix不采集远端URL。即使清洗凭据，远端仍可能暴露内部主机名、组织名和仓库路径；Coding Agent完成当前工作区任务并不需要该字段。

### 7.2 OpenCode

`packages/core/src/system-context/builtins.ts:12-42`把工作目录、Workspace根、是否Git仓库、平台和日期注册为独立上下文来源。`packages/core/src/system-context/registry.ts:24-43`拒绝重复键、按键稳定排序并并发加载来源。`packages/core/src/system-context/index.ts:5-17`明确来源可独立刷新，`48-80`定义持久快照，`182-205`以一次组合观测建立完整基线，`217-290`对更新、删除和暂时不可用执行reconcile。

可借鉴点是稳定来源身份、完整基线和可比较快照。并发加载并不自动提供文件系统级原子快照；Harnessix不能把“同一批协程完成”表述为“同一时刻状态”。

### 7.3 Claude Code逆向整理源码镜像

`src/context.ts:20,36-111`把Git状态限制为2000字符并明确标注为会话启动时快照；`113-189`把系统和用户上下文按会话缓存。`src/tools/AgentTool/runAgent.ts:400-410`对Explore/Plan子Agent删除陈旧Git快照，需要时由工具重新读取。该证据进一步说明Git时效必须显式定义，启动缓存和每模型步骤刷新不能混称为当前状态。

`src/main.tsx:354-379`明确指出Git命令可能经Hook和配置执行代码，因此只在建立信任后预取。`src/utils/git.ts:123-179`又对攻击者可控的`.git`、`commondir`和worktree反向引用进行校验。Harnessix据此不新增裸`subprocess git`路径，而是复用已有的固定可执行文件、固定参数、空全局配置、关闭系统配置/Hook/fsmonitor/外部diff、无交互、无分页、五秒超时和有界捕获的`GitReadRuntime`。

该镜像不是官方源码，只用于交叉验证风险，不作为单一契约依据。

## 8. 0.6.2b 设计结论

### 8.1 三类内建Source

1. `WorkspaceContextSource`只列出Workspace根和配置工作目录的一级可见条目，不递归读取文件正文；复用`Workspace`与`list_files`，保留deny-path、no-follow、单链接、目录revision、扫描上限和取消语义。输出为稳定JSON，条目、正文和截断标记均有界。
2. `GitContextSource`只复用`GitReadRuntime.status`，输出仓库标志、分支、HEAD、upstream、ahead/behind及有界状态条目；不读取远端URL、Git用户名、提交日志或任意Git配置。非Git目录成功输出`repository=false`，不是错误，也不伪装成未知。
3. `EnvironmentContextSource`只读取宿主显式allowlist中的键，并先拒绝Secret类名称、控制字符、异常类型、单值/总量越界；不枚举`os.environ`。平台与Workspace相对工作目录由实现生成，全部作为External trust数据进入结构化JSON。

三类Source每个模型步骤重新观测，不使用启动缓存。正文只存在于瞬时模型请求；Session仍仅保存revision、字节数、fragment ID和组合指纹。

### 8.2 跨来源一致性

单Source沿用0.6.2a的`ContextInspection v2`和单次观测。两个及以上Source采用`optimistic-double-observation/v1`：按固定注册顺序完成第一轮全部观测，再完成第二轮全部观测；只有每个Source的`workspace_scope`一致、两轮`source_revision`一致且同revision的完整观测正文一致时，才使用第二轮结果规划模型请求。

该算法证明一个有界观测窗口内没有被检测到的变化，不承诺跨文件系统与Git的事务原子快照。第二轮完成后外部状态仍可能变化；后续副作用继续依赖工具自己的revision、审批和效果核对，不能把Context freshness当成写入授权。

多Source结果使用`ContextInspection v3`并持久化`ContextConsistencySnapshot v1`，记录算法版本、两轮观测、来源数量和共同Workspace scope。来源revision变化报可重试`context_sources_changed`；同revision却返回不同观测视为来源违反契约，报不可重试`context_source_invalid`；Workspace scope不一致报不可重试`context_source_workspace_mismatch`。所有失败均发生在Provider请求之前。

### 8.3 数据与边界

- Workspace模型视图默认最多列出根和工作目录各64项，正文上限12 KiB；超出正文边界时从稳定排序尾部省略并显式标记截断。
- Git模型视图默认请求100项状态，正文上限16 KiB；保留Git运行时返回的真实`total_entries`并显式标记截断。
- 环境allowlist最多32项，单值最多1024字节，模型视图总量最多4 KiB。
- 三类Source都绑定宿主规范Workspace根和相同deny-path策略；配置不一致产生不同`workspace_scope`并失败关闭。
- 组合Fragment继续受Context Engine总片数和总字节边界约束。可选来源按Project、Workspace、Git、Environment优先级依次装入；预算不足只省略完整Fragment，不切断结构化JSON。

### 8.4 明确不采用

- 不递归生成完整目录树；大仓库会放大延迟和Context，已有`list_files/glob/grep`承担按需发现。
- 不把任意环境变量、Secret值、远端URL、Git用户名和提交历史放入模型上下文。
- 不并发观测后宣称原子一致；跨来源缺少共同事务边界。
- 不在来源暂时不可用时回退旧正文；当前Session没有持久化可验证正文。
- 不在0.6.2b修改Tool Result历史视图；该能力仍由0.6.2c单独完成。
