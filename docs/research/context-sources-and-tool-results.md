# Context Source、项目指令与 Tool Result 模型视图源码研究

- 更新日期：2026-09-07
- 适用范围：Harnessix Code 0.6.2
- 研究状态：0.6.2a 项目指令 Source 与 freshness 已形成实现依据；Workspace/Git/环境 Source 和 Tool Result 模型视图结论作为后续切片输入

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

`codex-rs/utils/output-truncation/src/lib.rs:14-31`提供带原始规模提示的中间截断；`34-107`在裁剪文本时保留媒体和加密内容；`109-197`按总预算处理多个内容块并显式报告省略项。`codex-rs/core/src/context_manager/history.rs:223-280`表明进入模型历史的工具输出会经过有界处理。

### 4.2 OpenCode

`packages/core/src/session/compaction.ts:12-15`设定摘要缓冲、保留量、Tool输出和摘要输出上限；`83-120`只在构造Compaction输入时把工具/命令输出裁至2000字符，原Session消息不因此原地改写。

### 4.3 Claude Code 逆向整理源码镜像

`src/utils/toolResultStorage.ts:131-199`先持久完整大结果，再返回预览和文件引用；`367-470`保存按Tool Use ID冻结的替换决策；`680-908`在消息级预算内选择大结果、持久化、替换，并保证后续请求字节一致。该实现强调两点：先有完整证据再裁模型视图；同一历史项的替换决策需要可恢复且稳定。

### 4.4 Harnessix 后续约束

0.6.2a不修改`ToolResultContent`或历史投影。后续Tool Result切片必须同时满足：

- Session原始完成Item不可被模型视图裁剪反向改写；
- 只有已成功提交的完整Artifact才能生成“预览+引用”；
- 引用必须绑定Thread、Workspace scope、Artifact ID、完整性和过期语义；
- 相同Item在Resume/Fork和重复规划时得到相同模型视图；
- Artifact发布失败不得静默丢弃完整结果后继续调用模型；
- 文本、结构化JSON、媒体、Patch/Process专用证据需分别定义，不能用字符串切片覆盖所有类型。

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
- Tool Result模型视图、Artifact自动归档和Compaction。

这些能力分别进入0.6.2b、0.6.2c和0.6.3，未实现前不得描述为生产完成。
