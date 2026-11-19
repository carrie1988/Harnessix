---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [protocol, agent]
related_adrs:
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
  - docs/adr/0131-session-baseline-for-pre-existing-workspace-changes.md
related_tests:
  - tests/agent/test_host_notice_identity.py
  - tests/protocol/test_rejection_protocol_v2.py
supersedes: []
---

# ADR 0173 公开的用户消息注明是否由Harnessix发出

- 状态：评审中
- 日期：2026-11-18
- 修订：Agent Protocol 2.0的`PublicTextContent`新增可选字段`origin`（加法，旧字段与其他文本的线上形状不变）

## 背景

Beta Issue #46（0.10.0b53 macOS）：fast-xml-parser任务里同一条node检查连续三次结果相同，Harnessix自动给模型发了一条
重复命令提示。导出后，这条提示与操作者的原始任务都是只有`kind`与`text`的`user_message`，按结构统计人工介入次数得到2，
实际只有1；审核方也容易把提示当作操作者追加的指令。

Harnessix的宿主说明（完成守卫、截断与空回复恢复、重复命令提示、预算与目录说明、会话基线、取消后的说明等）在模型历史里
就是用户消息，落为普通的用户消息Item；身份由各模块按Turn与步骤派生、重放确定。终端界面按这些身份把其中三种显示为“提示”，
导出（`data export`）与`exec --events-file`的公开事件没有带上这个区分。

## 决策

负责人2026-11-18选方案A（另有B：只在导出里标注；C：只在文档里说明“[Harnessix host notice]”前缀）。

1. **公开字段。** `PublicTextContent`新增`origin`，只在`kind=user_message`时出现：用户输入为`user`，Harnessix自动发出的说明为
   `harnessix`。其他种类的文本不带该字段，线上形状与此前逐字节相同。
2. **按身份判定，不看正文。** `agent.host_notice_identity`汇总全部宿主说明的身份派生规则（每步至多一条的按步骤或序号派生，
   上限为`Budget.max_steps`的上限1000；每Turn一条的直接派生），`is_host_notice(turn_id, item_id)`按Turn缓存。用户自己输入
   同样的文字仍是`user`。规则对旧会话同样成立，升级后旧会话的导出与事件也能区分。
3. **持久化不变。** 事件与Item内容的持久形状、Seal与重放都不变，`origin`只在公开投影里计算。
4. **登记约束。** 新增一种宿主说明时须在`host_notice_identity`登记；测试核对每个调用`host_notice_events`的模块都已登记。

## 后果

- 导出与事件流的使用方可以按`origin`统计人工输入，审核时区分操作者指令与Harnessix的自动说明，正文照旧完整保留。
- 不登记的新说明会被标为`user`；登记约束由测试兜底。
- 终端界面仍按原有映射显示各类提示，本次不改。

## 验证

- `tests/agent/test_host_notice_identity.py`：发出宿主说明的模块都已登记；按身份而不是正文判定；公开投影里宿主说明为`harnessix`、
  同样文字的用户输入为`user`、助手消息不带`origin`（修改前两条用户消息都只有`kind`与`text`）。
- `tests/protocol/test_rejection_protocol_v2.py`：其他Item的公开形状不变。
- 用报告人#46会话状态目录的副本真实导出：游标4（原始任务）为`user`，游标1035（重复命令提示）为`harnessix`，与报告中的表一致。
