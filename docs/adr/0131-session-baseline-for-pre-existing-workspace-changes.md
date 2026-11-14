---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, tools, product_config, product_ui, evals]
related_adrs:
  - docs/adr/0124-completion-guard-for-unattended-turns.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
related_tests:
  - tests/agent/test_session_baseline.py
  - tests/agent/test_session_baseline_git.py
  - tests/tools/test_workspace_changes.py
  - tests/product_config/test_session_baseline_wiring.py
  - tests/integrations/test_harbor_protocol.py
  - tests/integrations/test_harbor_adapter.py
supersedes: []
---

# ADR 0131 会话基线：记录并保护会话开始前已有的未提交改动

- 状态：评审中
- 日期：2026-11-11

## 背景

会话开始时，工作区里常常已经有未提交的改动：用户自己做到一半的修改，或任务环境预先放进去的文件。
模型在`git status`里看到它们时无从判断来历，容易把它们当成自己留下的垃圾，在收尾清理时还原或删除。

SWE-bench Multilingual（30题×2次）中的证据：

- `projectlombok__lombok-3009__jGA3Cpp`：任务镜像的`buildScripts/tests.ant.xml`带一处未提交改动。
  第30步`git status --short`显示` M buildScripts/tests.ant.xml`，第31步查看了它的差异，第38步在清理时执行
  `git checkout -- buildScripts/tests.ant.xml`。源码修复正确，试次只因这次还原失败。
- 同题另一次`projectlombok__lombok-3009__Y6DQ3R9`在第46至48步遇到同样的情况，先说要清理，随后改口不动它，
  最后通过。同一模型、同一提示得出相反的决定。
- 全部60个试次中另有11次还原或删除了不是自己创建的文件（没有影响结果），19个试次花步数排查某个文件是否原本就在。

产品此前没有“会话开始时的工作区”这一事实：

| 位置 | 比较基准 |
|---|---|
| 模型可见的`git_status`、`git_diff`、`git_review`（`GitReadRuntime`） | HEAD与Index |
| `harnessix exec`摘要的`changed_paths` | HEAD |
| Git交付基准（`ProductGitDeliveryBaseline`） | 交付时的HEAD、Index与同一状态观察的摘要，不是会话级事实 |
| 评测适配器导出补丁（`integrations/harbor/patch_export.py`） | 智能体开始前的工作区快照（临时索引`git add -A`后写树） |

只有评测适配器知道开始前的状态，而它的做法要向Git对象库写入对象。产品的Git只读端口不写仓库，
本机沙箱也禁止写`.git`，所以这个快照方式不能搬进产品。

## 决策

1. **观察方式。** 会话基线复用`GitReadRuntime`的状态观察：与`git_status`相同的仓库根校验、配置守卫、
   命令行和解析器。新增只读入口`workspace_changes(cancel, paths=())`；`paths`经字面pathspec传入，
   只来自宿主先前的状态观察，不接受模型输入。`CodingToolRuntime`暴露同名端口，
   `AgentRuntime(session_baseline=<端口>)`启用，默认`None`时行为不变。产品运行时传入自己的
   `CodingToolRuntime`；没有git可执行文件时端口返回`None`，机制不生效。
2. **记录时机。** Thread还没有运行过任何模型步骤、当前Turn处于`accepted`、Thread不是Fork、且尚无记录时，
   在进入模型循环之前观察一次。此刻工作区里不可能有本会话的改动。工作区干净、不是Git仓库或读取失败时不落事件。
3. **记录位置。** 记录是一条已完成的`user_message`宿主说明，身份
   `uuid5(turn_id, "harnessix.session-baseline/v1")`，紧跟用户的首条输入，属于模型的初始上下文。
   正文是一句英文说明加末行一条JSON记录：

   ```json
   {"spec_version":"harnessix.session-baseline/v1","head_oid":"<HEAD>","changes":[" M build/config.xml","?? notes.txt"],"total_entries":2,"truncated":false}
   ```

   `changes`逐条是`git status --short`的写法。宿主此后只读这一行。基线随Thread历史持久、重放确定；
   不新增事件类型、Thread字段、存储或产品配置字段。
4. **有界。** 至多记录64条、记录至多4 KiB。Git先列已跟踪文件的改动、后列未跟踪文件，截断时留下的是
   无法重新生成的部分。截断时`truncated`为`true`，说明里写明只列出前N条，并要求把其余“不是自己创建或修改的”
   文件同样视为已有。改动多到状态读取超出上限（1 MiB）时没有条目可列，说明改为“多到无法列出”，
   `total_entries`为`null`。
5. **标记。** `git_status`与`git_review`的成功结果中，路径属于基线的条目加`pre_existing: true`，
   状态对象加一句`pre_existing_note`。基线不完整时，说明里指出未标记的条目也可能先于会话存在。
   标记后超出结果上限时返回未标记的原结果。`git_diff`返回文本，不改。
6. **完成前复核。** 模型一步没有工具调用且回复未被截断时，在终结判定之前对基线路径做一次限定路径的状态观察。
   条目算作丢失的条件：路径不再出现在状态里（已还原，或未跟踪文件已删除），或开始前不是删除状态而现在是。
   HEAD已移动（提交、切换分支）或观察不完整时不判断。有尚未提示过的丢失路径时，落一条`user_message`宿主说明，
   身份`uuid5(turn_id, "harnessix.session-baseline-loss/v1")`，末行JSON列出这些路径；该说明与运行中Steering一样
   使本步重入。约束：每个Turn至多一条；任何Turn提示过的路径不再提示；步骤或Token预算用尽时不补发。
   用户明确要求还原时，模型多答一步即完成，后续Turn不再重复。
7. **界面。** Product UI不把这两条英文说明显示成用户发言。
8. **评测接入。** Harbor评测此前启动App Server时不带git，产品既没有Git只读工具，也无法建立基线。
   适配器新增`git_read_tools`（默认`false`）：开启后协议驱动用`harnessix code`、`harnessix exec`的同一探测
   找到git并传入`--git-executable`；探测不到按未配置启动。

## 被拒绝方案

- **开局把快照提交为本地commit，或写入对象库。** 模型的Git视图会与基线完全一致，但改变用户仓库的历史或对象库，
  与只读端口和沙箱对`.git`的写保护冲突。
- **新增事件类型或Thread字段。** 需要升级事件版本和公共Schema；宿主说明Item已经能持久、重放。
- **放进系统指令（动态Context Source）。** 指令每步重算，冻结的记录必须另设存储才能取回。
- **记录内容摘要，文件一有变化就提示。** 用户带着在途修改让模型接着改是常态，每次都提示是打扰；
  只在改动消失时提示。
- **宿主保存原内容并自动恢复。** 涉及内容存储、大小上限与敏感内容，范围过大。
- **只写进默认指令。** 模型仍要自己猜哪些文件属于“之前”，同一提示下已出现相反的决定。

## 后果

- 工作区有未提交改动的会话：首个请求多一条说明（通常数百字节，上限约4.5 KiB）；会话开始时和每次
  无工具调用的回复之前各多一次Git状态读取；改动丢失时多一个模型步骤。
- 工作区干净、不是Git仓库或未绑定git：不落任何事件，行为与此前相同。
- `AgentRuntime`新增一个可选参数，`CodingToolRuntime`与`GitReadRuntime`各新增一个只读方法；
  `runtime.py`与`CodingToolRuntime`的长度超过现行可读性预算，需要同步登记。

## 已知局限

- 模型用命令工具自行运行`git status`时看不到标记，只能依靠开头的说明与完成前的复核。
- 只判断“改动消失”；文件被改写成别的内容不提示。
- 宿主不保存原内容。模型没有读过的文件（例如被删除的未跟踪文件）无法恢复，只能在总结中说明。
- Fork出的Thread不记录基线。工作区是仓库子目录，或仓库配置含`filter`、`include`时，Git只读端口拒绝读取，机制不生效。
- 后续Turn里，开头的说明可能被上下文压缩的摘要取代；标记与复核不受影响。
- 未跟踪文件在会话中变为被忽略时会被当作丢失，提示一次。用户在两个Turn之间自己还原文件，
  下一个Turn完成前也会提示一次。
- 完成守卫同时开启、且本Turn没有成功补丁时，模型回应复核说明之后还会收到一次“尚未修改代码”的提示。

## 验证要求

- 记录：有改动、干净、读不到、条目过多、路径过长、改动多到读不出；只在会话动过工作区之前记录一次。
- 丢失判定：还原、删除未跟踪文件、已修改文件被删除、暂存或继续修改、HEAD移动、观察不完整。
- 标记：`git_status`与`git_review`；无可标记、失败结果、形状不符、超出上限。
- Runtime：首个请求包含说明；还原后提示一次并照常完成；用户要求的还原不在后续Turn重复；最后一个预算步骤不补发；
  事件重放与Thread一致。
- 真实Git仓库端到端：开始前已有一处已跟踪改动和一个未跟踪文件，会话中被还原和删除（上述失败试次的一般形态）；
  提交后不误报；非仓库工作区无事件。
- 观察端口：与`git_status`结果一致；路径限定按字面匹配且不受其他改动数量影响。
- 评测接入：`git_read_tools`只在开启时传到协议驱动；探测不到git时命令行不变。
