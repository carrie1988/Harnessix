---
doc_type: validation-evidence
status: current
version: 1
code_revision: 3fb2ef57f3a647b20058f987f090e6829c4d8a58
owners: [core]
modules: [models, evals, product_config]
related_adrs:
  - docs/adr/0008-provider-event-model.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/models/test_chat_text_tool_boundary.py
  - tests/models/test_chat_mapping.py
  - tests/models/test_tool_alias_identity.py
supersedes: []
---

# 合成原生工具协议真实探针

## 1. 结论与限制

`PASS_SCOPED_SYNTHETIC_NATIVE_PROTOCOL_NOT_BETA`。固定N候选及非editable Wheel，
北京`qwen3-coder-plus-2025-09-23`非Thinking/streaming模式，两次真实响应均为合法原生`read_file`，
第二次续页携带正确`expected_revision`。无助手文字、工具标记正文或实际工具执行。
这证明本合成条件下原协议可用，不证明线上普遍可靠，也不能倒推缺少wire的
[原BETA-001第四次分析](../beta-001-readonly-analysis-2026-10-07-v4/README.md)根因。

## 2. 预先冻结的计划

最多2个真实请求，步骤2、累计16,000 Token、180秒、每次最多1024输出Token、IO60秒、attempt1/retry0、
每步最多1原生调用及8192字节合成请求上限。原最高档费用预留算法、固定价格窗口和共享60元周期保持。
两次最高档预留合计40.32768元，预算可覆盖；新增unknown仍立即停止。原输入Token精确预检缺口不声称已修复。

仅广告原`read_file` Schema和别名，4行全部为无业务含义的合成fixture。第一页`succeeded`结果由宿主明确合成，
以原`Item/ToolCallContent/ToolResultContent`续入正式history；没有真的执行工具，也没有用户项目代码或配置外发。
首步无合法原生调用即停止，不发第二次、不解析XML、不重试。

## 3. 离线与实际观察

离线Mock native2步通过，文本标记对照首步拒绝且无第二请求；36件源绑定核验。
真实前置凭据可用且GET models200，未回显凭据。实际两POST均200：

| 步骤 | 原生结束 | 输入Token | 输出Token | 原生调用 | 正文 |
|---|---|---:|---:|---:|---:|
| 1 | `tool_calls` | 615 | 73 | 1 | 0字符 |
| 2 | `tool_calls` | 914 | 140 | 1，revision核对通过 | 0字符 |

总输入1529、输出213、累计1742；原Attempt与同周期reservation逐步唯一绑定，均completed。
线协议只保留字段类型、结束枚举、Usage、文本字符数/标记标志及别名摘要；不保存raw wire、模型正文或原参数值。

## 4. 原失败、OS策略与非变更

首次宿主策略不匹配预算原临时文件名，原reserve持久化在create/EPERM处拒绝。
没有ModelAttempt或wire、预算字节未变；不是供应商失败或未知费用。
独立私有clone沿原`_save`插入阶段观测，原selector及新增parent literal对照均失败；
精确锚定路径、显式32位hex和字面点形式通过原reserve/文件fsync/replace/目录fsync/settle及越界负控。
具体regex字符贡献未逐一隔离，不声称已证明某一字符原因。

新v2根只修正宿主OS selector，没有父目录写授权、整验证根权限或Guard算法变更；
旧v1不重跑，累计实际请求仍2。原主审预算元数据字段名导出错误单独保留，无额外请求或预算变化。
生产Adapter、别名、Schema、任务评分与执行权限不变；没有XML补执行或供应商解析修复。

## 5. 费用与证据

本次新增估算0.009524元；共享60元周期23请求全部completed，已知估算0.630272元、预留0、
剩余估算59.369728元，无新增unknown。实际发票/扣款未确认，不把估算称为实际结算。
旧两笔未决原件保持且不计新周期。证据见[事实](facts.json)、[评审包](REVIEW_PACKET.md)及摘要清单。

## 6. 后续门禁

不能以本样本标记有效客户源码分析、密码整改、完整SDK、R3质量或Beta完成。
完整分析和受控整改仍须另一个先冻结适用条件/预算/验收的计划；禁止同类分析盲试，禁止补造工具来源。
[原生来源详设](../../changes/m09-r3-native-tool-text-boundary.md)、
[隔离业务前测](../beta-001-isolated-baseline-2026-10-08-v1/README.md)和
[先导任务](../../operations/pilot-tasks/001-login-password-protection.md)分别保留各自结论。
