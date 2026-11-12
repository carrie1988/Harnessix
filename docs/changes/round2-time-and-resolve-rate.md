---
doc_type: change-design
status: current
version: 1
code_revision: ce11ed19cb6ae4c39d5a91112cf05da2aba90826
owners: [core]
modules: [agent, models, tools, delivery, patches, trusted_actions, context, session, product_config, product_ui, evals]
related_adrs:
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
  - docs/adr/0147-turn-stable-workspace-overview.md
  - docs/adr/0137-create-with-missing-parent-directories.md
  - docs/adr/0124-completion-guard-for-unattended-turns.md
  - docs/adr/0129-file-tool-leniency-and-correctable-rejections.md
  - docs/adr/0131-session-baseline-for-pre-existing-workspace-changes.md
  - docs/adr/0132-append-only-requests-and-token-calibrated-compaction.md
  - docs/adr/0133-parsed-projection-reuse-and-reducer-sharing.md
  - docs/adr/0134-default-working-habits-and-thin-eval-template.md
  - docs/adr/0135-command-ledger-step-notices.md
  - docs/adr/0136-readable-tool-aliases-and-model-facing-feedback.md
related_tests:
  - tests/agent/test_completion_guard.py
  - tests/agent/test_reply_shape.py
  - tests/agent/test_session_baseline.py
  - tests/agent/test_request_notes.py
  - tests/agent/test_compaction_calibration.py
  - tests/agent/test_step_notices.py
  - tests/agent/test_call_repair.py
  - tests/models/test_tool_alias_identity.py
  - tests/models/test_rejection_feedback.py
  - tests/product_config/test_working_habits.py
  - tests/product_config/test_command_ledger.py
  - tests/session/test_parsed_projection_cache.py
  - tests/tools/test_grep_include.py
  - tests/patches/test_planner_newlines.py
  - tests/context/test_turn_stable_source.py
  - tests/delivery/test_create_missing_parents.py
  - tests/product_config/test_patch_missing_parents.py
  - tests/product_config/test_patch_newlines.py
  - tests/models/test_tool_argument_repair.py
  - tests/agent/test_pressure_compaction_loop.py
supersedes: []
---

# 第二轮：任务耗时与解决率一起改进

## 1. 需求背景

2026-11-10在SWE-bench Multilingual上的正式对照（30题×2轮，模型qwen3.8-flash-next）里，Harnessix解决33/58个有效试次
（56.9%），对照组mini-swe-agent解决36/53（67.9%）；同样28道题Harnessix用时719分钟，对照组329分钟，是2.2倍。逐试次复盘
把差距归到产品自身的几类机制，而不是模型能力：

| 类别 | 现象 | 本轮对应改动 |
|---|---|---|
| 行为规则只写在评测任务模板里且被误用 | 规则被拿来缩小正确的修复，复现方式被写死在工作区之外 | 通用工作习惯进入产品默认指令，评测模板只留评测自身的事实（ADR 0134） |
| 完成判定漏判与误判 | 没写完的JSON被当作最终回复，任务在还有步数时以空补丁结束 | 完成守卫第2版（ADR 0124） |
| 工作区里会话开始前就有的改动被还原 | 模型把环境预先改动的文件当作垃圾清理掉 | 会话基线（ADR 0131） |
| 工具协议摩擦 | 工具名带32位摘要、参数被拒后整份重发、中文反馈、搜索返回看似完整的空结果 | ADR 0129、ADR 0136 |
| 每次请求重复计算提示词 | 每次请求末尾附宿主说明，过早压缩 | 请求只追加、压缩点按真实Token校准（ADR 0132） |
| 宿主每步自身开销 | 每步约1.4秒，随历史增长 | 投影解析复用与Reducer结构共享（ADR 0133） |
| 已观察到的事实没有告诉模型 | 同一补丁反复应用又回滚 | 命令账本与工具步骤说明（ADR 0135） |

产品负责人为本轮定了三条原则：不为评测过拟合；改动落在产品上，对所有用户生效；任务耗时与解决率同等重要。

## 2. 设计目标与非目标

目标：

- 每项改动是通用机制或通用默认指令，不含仓库名、语言特例或来自标准答案的信息。
- 评测运行的就是产品默认配置；评测与产品的已登记差异只剩无人值守必需的`completion_guard`与每步输出上限。
- 模型看到的反馈（工具错误、宿主说明、指令）是英文、简短、说明下一步；界面给用户看的文字不变。

非目标：

- 不改变公共协议、事件版本、产品配置Schema或`spec/`下的任何合同。
- 不改变补丁事务的原子性、摘要前置条件、审批绑定与回滚恢复。
- 不为单道评测题写规则；只在一道题上出现的现象不单独成为指令。

## 3. 总体架构

八个互相独立的增量，各有一份ADR，接线点集中在`AgentRuntime`与产品组合根：

| 增量 | 新增或主要改动的模块 | 接入点 |
|---|---|---|
| 默认工作习惯 | `product_config/agent_context.py`的`_WORKING_HABITS`片段 | Context运行时指令 |
| 完成守卫第2版 | `agent/reply_shape.py`、`agent/completion_guard.py` | 无工具调用的回复收尾前 |
| 会话基线 | `agent/session_baseline.py`、`tools/git.py`的`workspace_changes` | Turn接受时、无工具回复收尾前、Git读工具结果 |
| 文件工具放宽 | `agent/call_repair.py`、`agent/file_observations.py`、`tools/argument_repair.py` | 调用形成前的修复与补全 |
| 可读工具别名与英文反馈 | `models/_history.py`、`agent/tool_rejections.py`、`agent/planning.py` | 两个模型适配器的请求与历史映射 |
| 请求只追加、压缩校准 | `agent/request_notes.py`、`agent/compaction_calibration.py` | 每步准备历史之前、压缩规划 |
| 宿主开销 | `session/parsed_projection_cache.py`、`agent/reducer.py` | 会话存储读取、事件归约 |
| 命令账本 | `product_config/command_ledger.py`、`agent/step_notices.py` | 每步工具结果落账之后 |

## 4. 流程与数据流

一步之内的顺序（新增环节加粗）：

1. Turn被接受：**会话基线观察一次工作区**，有既有改动时补一条宿主说明。
2. 准备历史：**到达预算关口或可调用工具集变化时，把说明写进历史**；**按上一次请求的真实输入Token校准压缩点**。
3. 请求模型：工具以**可读别名**广告；历史里**被退回的非答复以占位符出现**。
4. 收到回复：**补丁调用缺的摘要、权限由宿主可确定时补全**；**计划调用排到调用组末尾**。
5. 执行工具并落账：**命令账本据持久事实推导说明**，有事实时补一条宿主说明。
6. 无工具调用的回复：**先复核会话基线里的改动是否还在**，再由**完成守卫按回复形态与状态判定**是否结束。

所有宿主说明都是持久的用户消息Item，经`host_notice_events`发布，请求因此始终是“上一次请求加新内容”。
系统指令在一个Turn内也保持不变：工作区一级目录概览每个Turn只在首个模型步骤观察一次（ADR 0147）；
开发集60个试次里系统指令在Turn中途变化26次，全部来自这段概览，每次都让服务端重算整段历史。

## 5. 接口设计

- `AgentRuntime`新增四个可选构造参数：`step_notices`、`session_baseline`、`history_notes`、`compaction_window`；默认值保持原行为。
- `ToolStepNoticeSource.step_notice(thread, turn)`与`WorkspaceChangeObserver`是两个新端口，实现分别在产品配置层与读工具运行时。
- `fill_patch_preconditions(proposal, definitions, workspace, history)`取代原先只接受读取函数的签名。
- Harbor适配器新增选项`git_read_tools`（默认关闭），评测组别显式开启后产品获得与用户默认相同的Git读工具与会话基线。

## 6. 数据结构与领域契约

- 不新增事件类型、Thread字段、存储表或配置字段。
- `git_status`与`git_review`结果可以多带`entries[].pre_existing`与`pre_existing_note`，在工具自身输出校验之后附加，工具描述与指纹不变。
- grep合同规则记录了新的`include`语义，grep描述符版本与指纹随之变化；`update_plan`的工具说明改为英文，其指纹变化。
- 模型线协议上的工具别名由“语义片段加摘要”改为“只替换非法字符，冲突时才加8位摘要后缀”；会话只保存注册原名，旧会话无需迁移。
- 配置向导对所有端点预设`normalize_tool_arguments=true`（原先只对自托管端点）：字段与Schema不变，只改向导生成的预设值；
  已有配置文件不迁移。依据是Beta真实使用（云端同族模型）里20次补丁参数拒绝的回放：只做结构修复有7次通过，加上还原后12次通过。

## 7. 失败、恢复、取消与超时

- 会话基线的任何读取失败都按“未启用”处理，不写事件，不阻塞Turn。
- 完成守卫的补发次数有与状态无关的硬上限（每Turn 6次），因此必然终止。
- 命令因超时被终止时，结果明确说明命令没有执行完、复合命令里尚未开始的部分没有运行。
- 压缩校准只放宽固定比例得出的数值，不超过出站请求扫描容量；服务商少报用量时由既有的超窗后被动压缩兜底。
- 工具步骤说明在工具结果提交后单独提交：进程在两次提交之间退出时该步说明丢失，但不会重复。
- 自托管服务的单次坏响应不再终止Turn：流式工具调用序号空缺时按到达的调用成组，主动压缩的摘要输出不成立且用量完整时
  跳过本次压缩（ADR 0109第4版）。开发集基线60个试次里有3个因此整题终止。

## 8. 持久化与事务

- 宿主说明与工具结果一样进入事件日志与Thread投影，重放结果不变。
- 被退回的回复只在发给模型的视图里换成占位符，持久Item、检查摘要与重放不变。
- 会话存储在投影字节的SHA-256与投影版本都未变化时复用已解析的Thread；每次读取仍取出行、两侧求摘要、校验封条、事件数与尾部前缀。
- 补丁事务的原子性、摘要前置条件、审批绑定与回滚路径不变（ADR 0129第3节）。精确编辑的后像自ADR 0129第2版起
  沿用被替换处的换行风格（CRLF处写CRLF，LF处写LF），匹配在逐字节失败后忽略CRLF/LF差异再试一次；
  此前版本保存的补丁计划按保存时的语义复核，升级后仍可回滚与恢复。
- 新建文件的成员在同一事务内补建缺失的父目录（ADR 0137，POSIX）：目录不成为计划成员，由计划自身的来源快照推导；
  成员在文件就位前失败时只删除它补建的空目录；回滚该补丁时删除当初补建且已为空的目录。审批Diff与界面列出将新建的目录，
  公共合同与Schema不变。已发布前缀不自动回滚这一既有语义（ADR 0068）不变。Windows行为不变，仍需先建目录。

## 9. 安全、权限与信任边界

- 写成文本的工具调用永远不执行：调用只从服务商的调用通道形成，文本可能只是在引用工具输出或文件内容。
- 被拒调用的名字与参数不进入历史；反馈不回显路径、取值或文件内容，只给出`files[i].edits[j]`这样的位置，
  并写明整批都没有写入（批次是原子的，同批其他文件也没有应用）。
- 出站请求的公开保护扫描、历史校验与审批绑定没有放宽。
- 读取方共享已解析的Thread对象，任何代码不得就地修改其中的JSON容器；这一约束是ADR 0133的前提；产品负责人于2026-11-12批准保留这两项改动。

## 10. 可观测与错误分类

- 新错误码`tool_no_files_searched`：`include`没有匹配`path`下任何文件。原先这种情况返回看似完整的空结果。
- `glob`的模式以不存在的具体目录开头时返回`tool_not_found`。
- 评测里可按宿主说明的固定前缀统计各类说明出现的次数；`evals/suite.py`把它们计入`steering_messages`。
- 界面对失败原因显示的中文文案由错误码映射，不随英文反馈变化。

## 11. 测试与验收

- 各增量的单元测试与由真实案例约简的回归测试见front matter的`related_tests`。
- 合并后在本机运行全量测试，失败集合与基线提交`acd37114`逐条比较，只允许环境原因的既有失败。
- 效果验收在评测上进行：新抽取的Multilingual开发集（30题）用于对照，最终结论以从未用于分析的测试集（30题）为准，同时报告解决率与耗时。

## 12. 源码映射与阅读顺序

1. `product_config/agent_context.py`：`_WORKING_HABITS`。
2. `agent/reply_shape.py` → `agent/completion_guard.py`。
3. `agent/session_baseline.py` → `tools/git.py`的`workspace_changes`。
4. `agent/call_repair.py` → `agent/file_observations.py` → `delivery/trusted_action_contracts.py`。
5. `models/_history.py`的`tool_aliases`、`wire_directory`与`_public_result`。
6. `agent/request_notes.py` → `agent/compaction_calibration.py` → `product_config/context_budget.py`。
7. `session/parsed_projection_cache.py` → `agent/reducer.py`。
8. `product_config/command_ledger.py` → `agent/step_notices.py`。

## 13. 部署、兼容与回退

- 没有数据迁移；旧会话可直接继续。带摘要的旧工具别名只存在于请求映射中，不在会话里。
- 每个增量是一次独立合并，可以单独回退；ADR 0133的两项改动各自独立提交。
- 评测运行规格需要：任务模板改用`swebench-task-v5.j2`（v4加两条评测事实：测试数据不得增改、环境离线；ADR 0134第2版），组别开启`git_read_tools`，累计Token预算上调（压缩变少后长任务的累计输入变多）。

## 14. 风险与取舍

- 所有效果数字来自上一次评测的回放或估算，没有经过模型实跑；以开发集与测试集的对照结果为准。
- 命令账本的“通过后失败”说明在已记录的60个试次上一次都不会触发：模型很少重复同一条命令，且常用管道掩盖退出码。保留它是因为新的默认习惯要求改动前后运行同样的测试，实际价值待评测确认。
- 允许在工作区内写临时测试后，跑到步数或时间上限的任务可能把临时文件留在工作区里。
- 模型第一次在产品路径上收到剩余预算的说明，也不再在每次请求末尾读到工具目录说明；两者都可能改变收尾行为。
- 宿主说明在界面上不再显示成用户发言：会话基线与请求关口说明不显示；完成守卫、截断恢复与命令账本的说明各显示为
  一行“提示”加一句中文，英文正文只给模型看。
