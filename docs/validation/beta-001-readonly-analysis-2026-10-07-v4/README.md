---
doc_type: validation-evidence
status: current
version: 1
code_revision: 3fb2ef57f3a647b20058f987f090e6829c4d8a58
owners: [core]
modules: [sdk, product_config, agent, evals]
related_adrs:
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_coding_workflow_instructions.py
  - tests/product_config/test_agent_context.py
  - tests/tools/test_search_kernel.py
  - tests/tools/test_search_boundaries.py
  - tests/agent/test_usage_observations.py
supersedes: []
---

# BETA-001 原生工具标记文本与任务技术门拒绝

## 1. 结果与边界

`HOLD_NATIVE_TOOL_TEXT_AND_REQUIRED_SOURCE_COVERAGE`。第四次独立计划没有因预算耗尽失败，
但只读一个文件的首页即以正文结束；宿主技术门拒绝该完成Turn，不计有效分析、整改或Beta完成。
[前三次失败](../beta-001-readonly-analysis-2026-10-07-v3/README.md)及其原限制保持；本次不是v5单变量对照。

## 2. 原件与执行身份

仍固定同一3fb2ef5候选、SHA256为 `751b3a67ba6978e4729a4b531b01b7bd3d7ddb7805559419b0dca838e277a584` 的独立Wheel，
以及12件已审阅初始输入；无整改结果、答案或原环境数据。明确列出5个核心链文件，要求真实读取与行号绑定。
本次独立20步/250,000累计Token/180秒、Provider最多3072输出Token；不更改产品默认或前三次预算。
完整SDK/协议/原Session/WorkspaceScope仍复用，只广告5个只读工具；无写入、测试或业务API。

## 3. 实际观察

- 两次模型请求完整结算；累计输入9,328、输出445、共9,773 Token，未耗尽本计划步数/Token/期限。
- 一次成功 `read_file` 只返回首200行，`truncated=true`、`next_line=201`；全文件摘要完整不意味着全部正文已读。
- 末段292字符为函数/参数XML形态工具标记，缺少开头 `tool_call` 标记；不是源码分析结论。
- 最后文字没有对应原生 `ToolCallCompleted` 或真实续页工具效果；客户端没有将文字解析成工具调用。
- Runtime Turn为 `completed`；新私有任务门检查核心5路径仅1/5，宿主以 `verification_required_source_coverage_missing` 退出1。

必须分清Provider请求完成、Turn协议完成和任务验收完成，不能因前两项完成而认为分析通过。
工具标记文本不作为工具来源，不能补造原生事件或自动执行其中参数来消除失败。

## 4. 协议求证与未确认归因

[百炼官方工具调用示例](https://help.aliyun.com/zh/model-studio/qwen-coder)通过响应的 `tool_calls` 读取结构化函数调用并执行工具；
[Qwen官方模型解析器源码](https://huggingface.co/Qwen/Qwen3-Coder-480B-A35B-Instruct/blob/main/qwen3coder_tool_parser.py)展示该模型族函数/参数标记解析。
两者只帮助识别协议边界，不证明当前托管服务使用了该解析器，也不证明本次问题必然来自供应商或工具别名。

原wire正文未保存，本次直接证据是认证Session中的原生调用与最后文本。
[Chat流解析](../../../src/harnessix/models/_chat_stream.py)、[请求与别名映射](../../../src/harnessix/models/_chat_mapping.py)
及[Runtime完成分支](../../../src/harnessix/agent/runtime.py)均未修改。下一步先做合法原生工具与相同标记文本的离线控制及有界归因，
不通过静默客户端XML执行、换模型、隐藏重试或第五次同类真实运行消除失败。

## 5. 费用、持久化和隐私

本次估算0.044432元；四次共21个完成请求，新60元共享周期累计估算0.620748元、预留0、无新增unknown。
旧两笔费用保留且不阻塞新周期；实际账单仍未确认，估算不是发票结算金额。
输入12件前后摘要及身份相同，重开/Replay一致。原Owner、原Key/Scope与认证历史读取不初始化、不调用模型或预算Owner。
受保护主库/材料/收据/宿主/清单及新旧账本摘要一致；不承诺共享内存侧文件或原业务目录全部正文长期未变。
主审元数据导出完成后出现控制台汇总字段名错误，原件另存；不是产品失败、额外模型请求或诊断正文缺失。
公共资料无客户代码、模型正文、完整Prompt、密钥或个人路径；私有运行/认证元数据单独封存。

## 6. 后续门禁

[任务登记](../../operations/pilot-tasks/001-login-password-protection.md)、[单人先导手册](../../operations/pilot-beta.md)、
[事实清单](facts.json)及[评审包](REVIEW_PACKET.md)保持真实任务完成0。
完整有效分析、业务输入处理、实际Patch审批、隔离回归、浏览器验证、取消/恢复、人工验收及R3/三平台/商用门槛仍开放。
