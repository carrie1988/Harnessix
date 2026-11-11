---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, product-config]
related_adrs:
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
  - docs/adr/0124-completion-guard-for-unattended-turns.md
related_tests:
  - tests/agent/test_step_notices.py
  - tests/product_config/test_command_ledger.py
supersedes: []
---

# ADR 0135 命令账本与工具步骤说明

- 状态：评审中
- 日期：2026-11-11

## 1. 背景

SWE-bench Multilingual复盘（30题×2轮，产品版本447fe024）的根因2是“修好又改坏”：模型已经写出正确版本，
之后继续修改，改坏后把失败归咎于环境。宿主持有每条命令的结果和每次补丁事务，却从不说明。

- google__gson-1014__TpMBBdg：step 12补丁正确，step 15的补丁把缺陷放了回去，step 16起模型认为是
  “stale classes”，用44步排查编译缓存和字节码。
- laravel__framework-48573__PZDTTT5：同一份Diff应用后回滚6次（step 25/26至89/90），随后约50步重复
  同一组验证命令，直到步数用尽。

用录得的60个试次回放了本ADR的判定（1473次正常退出的Shell命令运行：1356次以0退出，117次非零）：

| 事实 | 数量 |
|---|---|
| 规范化后不同的命令文本 | 1430；只有25条文本被再次运行（共43次） |
| 再次运行的结果变化 | 通过→通过40，失败→通过2，失败→失败1，**通过→失败0** |
| 非零退出 | 117次，没有一次发生在此前通过过的命令文本上 |
| 以输出整形管道或`echo`结尾的命令（粗略匹配） | 828次，约56% |

也就是说，在这批记录上“同一条命令通过后失败”一次都没有发生，原因有两个：

1. 模型几乎不重复同一条命令文本（每次改写脚本名、过滤器或拼接的子命令）；
2. 检查的退出码常被掩盖：`… | tail -20`、`…; echo exit=$?`使命令总以0退出，失败只写在输出里。
   gson-1014 step 13与step 16两次都以0退出，fmt-2457 tk2eakD step 83与step 87是同一条命令、
   其间有补丁、输出从正常变为异常，退出码仍都是0。

其余三个“修好又改坏”试次（fmt-3729 wgLG5JX、prometheus-11859 MULMDxt、rubocop-13705 w64GyQh）在
会话里根本没有“改之前通过、改之后失败”的检查：被改坏的行为从未被运行，或前后都通过。宿主对它们无事实可说。

因此本ADR的回归说明是一项对真实用户成立的产品机制（用户让Agent反复跑同一条测试命令是常态），
但不能指望它在上述记录上改变结果；补丁反复与原地重复两类说明在laravel-48573 PZDTTT5上各自成立。

## 2. 决策

### 2.1 命令账本（`product_config/command_ledger.py`）

每步工具结果全部落账后，从本Thread各Turn的原始Item推导三类事实，只为刚结束的这一步给出：

| 事实 | 判定 |
|---|---|
| 回归 | 同一工具、同一规范化命令文本的**上一次**有退出码的运行以0退出，其后至少一次补丁或回滚成功，本次以非零退出 |
| 补丁反复 | 一次成功的回滚所指向的补丁，其Diff摘要（`diff_sha256`）已被回滚过一次，即同一份内容第2次被回滚 |
| 原地重复 | 同一条命令第3次得到相同退出码与相同的stdout/stderr观察摘要，其间没有补丁或回滚成功 |

- **只看上一次运行**：通过→（无补丁）失败→补丁→失败不说明，第一次失败早于补丁；通过→补丁→失败说明一次，
  之后继续失败不再说明，直到命令重新通过。没有通过过的命令永远不说明。
- **命令**：只含`exec_command.*`与`host_command.*`。规范化只去掉首尾空白、行尾空白和换行符差异，
  行内空白可能有语义，不折叠。固定Profile（`run_profile.*`）流程另有门禁说明，不进账本。
- **退出**：`stop_reason`为`exited`且带退出码才算。超时或被终止的运行不更新账本。
- **工作区变化**：成功的`apply_patch_batch`与`rollback_workspace_patch`。Shell命令自己对工作区的修改
  宿主没有观察，不计入；原地重复要求输出逐字节相同，以此排除这类变化带来的新信息。
- **只陈述事实**：通过发生在第几步、其间补丁的`transaction_id`与文件路径、最近一次补丁可用
  `rollback_workspace_patch`撤销或用新补丁收窄（仅当回滚工具通过能力验证）。不判断因果，不建议修法。
- **有界**：命令摘录至多200字符（保留首尾），最多列最近3次补丁、每次2个路径，更早的只报数量。

### 2.2 发布（`agent/step_notices.py`）

内核新增端口`ToolStepNoticeSource.step_notice(thread, turn) -> str | None`，`AgentRuntime`在`_drive`里
`_execute_calls`返回之后调用一次。有事实时追加一条用户消息Item：

- 正文以`[Harnessix host notice] `开头，身份`uuid5(turn_id, "harnessix.tool-step-notice/v1:<step>")`，
  复用完成守卫的`host_notice_events`与Event v19的Steering通道，不新增事件类型或版本；
- 经`_commit`提交，公开输出保护照常生效；
- 每个Turn至多6条、每条至多2000字符；同一步已发布则不再询问来源，恢复重入不重复。

### 2.3 说明为什么不写进工具结果

任务最初的设想是把说明追加到那条工具结果里。当前合同下做不到而不破坏既有校验：

- 命令结果的`output`与输出Artifact逐字段绑定，`validate_action_output_body`要求两者完全一致，
  多一个键即`artifact_corrupt`，模型随后无法`read_artifact`读取该命令的完整输出；
- 补丁结果的`output`与Router审计摘要绑定；
- 给`ToolResultContent`加字段需要新的Agent Event版本（当前v22）及其兼容处理，超出本轮范围。

用户消息Item同样是持久历史的一部分，后续请求只在末尾追加；压缩时当前Turn的用户消息整组保留（ADR 0124
第3.3节），所以说明在压缩后仍在模型上下文里，判定本身读的是原始Item，不受压缩影响。

### 2.4 配置

`AgentRuntime(step_notices=...)`默认不注入。产品在`exec_command`或`host_command`通过能力验证时注入
`command_ledger_notices(verified)`，没有新增产品配置字段，公开合同与Schema不变。

## 3. 被拒绝方案

- **按输出判断回归**（比较前后输出里的`OK`/`FAIL`行）：能覆盖gson-1014那一步，但要猜测输出格式，
  容易误报，且判定规则来自个别试次。需要产品负责人单独决定，本ADR不做。
- **与最近一次通过比较**（而不是上一次运行）：补丁之前就已失败的命令会被归到补丁上。
- **只按退出码判断原地重复**：轮询服务状态、`git status`这类命令会被误报。
- **进程内账本**：重启或恢复后丢失；从持久Item推导不需要新状态。
- **强制回滚或禁止二次编辑**：复杂任务需要迭代，宿主只说明。

## 4. 后果

- 用Shell命令反复跑同一检查的会话，在检查由通过变为失败的那一步得到一条带补丁标识的说明。
- 检查的退出码被管道或`; echo`掩盖时，回归说明不会出现。这是已知边界，测试
  `test_a_check_that_hides_its_exit_status_is_out_of_reach`固定了它。
- 终端界面目前把宿主说明显示为一条用户消息行（与截断恢复、完成守卫的说明相同）；是否隐藏未在本ADR决定。
- 每步多一次对Thread Item的线性扫描，回放实测平均0.17毫秒。
- `runtime.py`增加6行，需要同步可读性策略中的文件与热点上限。

## 5. 验证

- `tests/product_config/test_command_ledger.py`：三类事实的判定与各自“只说明一次”、从未通过不说明、
  失败早于补丁不归因、超时不算退出、失败的补丁不算变化、有界、不依赖压缩窗口、跨Turn与重启后仍成立，
  以及“应用又回滚6次后原地验证”会话的归约形态。
- `tests/agent/test_step_notices.py`：前缀、每步一次、每Turn上限、长度上限、关闭中的Turn不发布；
  Runtime把说明放在该步全部工具结果之后，Provider消息序列合法，事件重放与Thread一致。
- 回放60个录得试次：1个试次得到3条说明（laravel-48573 PZDTTT5：step 32补丁反复，step 70、75原地重复），
  回归说明0条，其余59个试次0条。
