---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [models, agent, product_ui]
related_adrs:
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
  - docs/adr/0136-readable-tool-aliases-and-model-facing-feedback.md
related_tests:
  - tests/agent/test_truncation_recovery.py
  - tests/models/test_tool_rejection.py
  - tests/models/test_rejection_feedback.py
  - tests/product_ui/test_rendering_host_notices.py
supersedes: []
---

# ADR 0171 输出在工具调用中途用满上限时，告诉模型拆成更小的调用

- 状态：评审中
- 日期：2026-11-18
- 修订：Provider事件`ResponseCompleted`新增`tool_call_truncated`；运行时补发一条宿主说明；非法参数拒绝的英文反馈补一句

## 背景

模型一次回复写满输出上限（例如16384 Token）时，如果正在写工具调用，调用参数被截断。ADR 0109第5版规定：
被截断的调用按`invalid_arguments_json`拒绝、不执行，同一响应里完整的调用照常形成，终态改记`tool_calls`，
Turn在原预算内继续。

这时模型收到的说明是“参数不是合法JSON，请重发完整、正确转义的参数”。它说的不是原因：模型多半原样再写一遍
同样长的调用，再次被截断。2026-11-17 NL2RepoBench正式评测（wsgidav）中，模型一次回复写满16384 Token、
用时约9.5分钟，调用被截断后只得到这条说明。本机部署的模型常见4096或8192的输出上限，真实用户遇到的次数会比
评测多。

负责人2026-11-18批准（第5项选A）：回复写满输出上限、工具参数被截断时，提示改为“输出已达上限，请拆成更小的
批次分次提交”。

## 决策

1. **适配器标出截断。** OpenAI兼容适配器以`length`结束、已形成调用组且组内有被拒绝的调用时（即ADR 0109第5版
   改记`tool_calls`的情况），`ResponseCompleted.tool_call_truncated`为真，其余情况恒为假。Provider事件Schema
   （`provider-event-v4`）只新增这个带默认值的字段。Anthropic端点以`max_tokens`结束且带半截工具调用时仍按原规则
   失败（ADR 0109第5版“未覆盖”），不在本ADR范围。
2. **运行时补发一条宿主说明。** 截断恢复开启时（产品默认开启），`settle_reply`对带该标记的终态返回新情况
   `truncated_call`；本步没有待执行的调用时，`_close_model_step`补发宿主说明`TRUNCATED_CALL_NOTICE`：原因是输出上限，
   原样重发会再次被截断，应拆成更小的调用（例如一次补丁少放几个文件，或分几次写一个大文件）。
   - 说明落为一条已完成的用户消息Item，身份`uuid5(turn_id, "harnessix.truncated-tool-call-notice/v1:<step>")`，
     重放确定；与截断、空回复补发的身份互不相交，**不计入连续补发次数，也从不使Turn失败**：被截断的调用本来就
     在原预算内继续，步数预算照旧是上限。
   - 不新增事件类型；拒绝事实与固定拒绝结果（`tool_arguments_invalid_json`）不变。
3. **拒绝反馈补一句。** `invalid_arguments_json`在模型线协议上的英文反馈（ADR 0136）在“请重发完整、正确转义的参数”
   之后补上：调用很长时多半是回复写满了输出上限而被截断，应拆成更小的调用，不要原样重发。同一响应里还有完整的
   调用时，本步要先执行它们，第2条的说明不补发，这句反馈覆盖这种情况。
4. **界面。** 终端界面把这条说明显示为一行“提示”：“模型写工具调用时用满了输出上限，调用被截断未执行，已提示它拆小重发”，
   不显示英文正文（与其他宿主说明相同，按派生身份识别）。

## 没有选的方案

- **新增拒绝原因（例如`truncated_arguments`）。** 要同时改内部事件、公共协议v2与客户端的原因枚举。ADR 0109第2版
  的经验是：旧客户端遇到不认识的原因会校验失败并取消回合。用已有的宿主说明机制可以得到同样的效果，不改会话事件
  格式。
- **只改拒绝反馈的措辞。** 模型无从判断这一次是不是截断；适配器知道，应当明确告诉它。

## 后果

- 截断发生时模型多收到一条说明，下一次请求多约80 Token。
- 已有会话恢复时，历史中`invalid_arguments_json`拒绝的反馈文本按新措辞渲染，一次性改变这部分前缀。
- 关闭截断恢复时只有第3条生效，其余行为与此前相同。
- 三个平台相同。
