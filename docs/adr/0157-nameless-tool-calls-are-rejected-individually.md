---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [models]
related_adrs:
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
  - docs/adr/0008-provider-event-model.md
related_tests:
  - tests/models/test_tool_rejection.py
  - tests/models/test_chat_empty_name.py
  - tests/models/test_chat_terminal_diagnostics.py
supersedes: []
---

# ADR 0157 名称缺失或超长的工具调用只拒绝该次调用

- 状态：评审中
- 日期：2026-11-13
- 修订：[未知工具恢复设计](../changes/m09-r3-unknown-tool-recovery.md)中“空名/过长名不是可恢复目录错误”的规定

## 背景

Beta用户在0.10.0b24上的反馈（GitHub Issue #1）：用阿里云百炼`qwen3.8-flash`从空仓开发两个带测试的Python工具，
两次任务分别在约5分12秒和14分59秒后结束，界面显示“未完成”：

```text
错误：Provider 返回结构化失败（provider_invalid_provider_output）
```

模型尝试记录里的原因是`chat_protocol/v1:tool_name_unknown`。已经写好的代码都在，但要用户再发一次“继续”才能接着做，
无人看管的长任务就停在这里。

`tool_name_unknown`只在一种情形产生：模型给出的工具调用没有可用的名称——名称始终没有到达、是空字符串，
或者超过256个字符。另外两种“模型写错了”的情形早已不使整轮失败：

- 名称不在本次工具目录里：只拒绝这一次调用，模型看到“工具未注册”和当前可用的工具清单后改正；
- 参数不是合法的JSON对象：同样只拒绝这一次调用（[ADR 0109](0109-recoverable-truncation-and-read-guidance.md)）。

只有名称缺失这一种仍按协议错误处理：整个请求失败且不可重试，任务随之中断。

## 决策

1. **名称缺失、为空或超过256个字符的调用，与目录外的名称同样处理。** 只为这一次调用产生
   `ToolCallRejected(reason="unregistered_tool")`，同一个响应里其他完整的调用照常放行。
   OpenAI Chat与Anthropic两个流式适配器一致。
2. **超长的名称不保留原文。** 流状态里只留下一个不可能被登记的占位，原文不进入事件、历史或日志。
3. **其余结构性错误不变，仍使请求失败**：缺ID、ID重复或超长、index有缺口、类型不是function、
   名称在分片之间变化（包括超长名称之后又出现另一个名称）、没有终态、没有用量。
   Anthropic协议里`name`字段缺失或不是字符串，仍在帧校验阶段失败。
4. **模型看到的内容沿用原有的拒绝路径**：历史里这次调用以`harnessix_rejected_tool_v1`占位，
   配对结果是“工具未注册”，下一次请求附带当前可用的工具清单。

## 取舍

- **安全性。** 没有名称的调用没有可以执行的对象；拒绝事件不带名称、参数或执行能力，与目录外名称的拒绝完全相同。
  原设计的顾虑是“畸形的一组调用不释放任何提案”——结构残缺的响应里，其他调用是否可信。
  这里的响应有完整的终态和用量，每个调用的ID与类型都通过了校验，只是模型没有给出名称；
  同组的其他调用各自通过同样的结构校验。目录外名称、畸形参数已经按逐次拒绝处理并与正常调用并存，
  本决定把最后一种“模型写错了”的情形并入同一条规则。
- **没有新增拒绝原因。** 单独的“名称缺失”原因要改Provider事件、Agent领域模型、公共协议与三份Schema，
  并与第二轮开发正在改的拒绝合同（被拒工具名）冲突。模型从“工具未注册”和工具清单已经能改正。
- **诊断词汇保留。** `tool_name_unknown`留在枚举里：历史会话和评测记录中仍有这个原因；新版本不再产生。
- **反复出错。** 模型连续给出无名调用时，每次都只多花一步，由本轮的步骤预算终止。

## 后果

- 百炼等端点偶发的无名调用不再中断任务，代价是多一次模型请求。
- `docs/modules/models.md`第11.1节“最终仍缺名则整批拒绝”的描述，以及两份历史变更设计里的同一规定，随本ADR更新。
