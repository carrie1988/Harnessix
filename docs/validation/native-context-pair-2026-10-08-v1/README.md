---
doc_type: validation-evidence
status: current
version: 1
code_revision: 3fb2ef57f3a647b20058f987f090e6829c4d8a58
owners: [core]
modules: [models, context, evals]
related_adrs:
  - docs/adr/0008-provider-event-model.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/models/test_chat_text_tool_boundary.py
  - tests/models/test_chat_mapping.py
supersedes: []
---

# 五工具与产品Context的单变量合成配对

## 1. 背景与结论

`PAIR_OBSERVED_EXPLORATORY_ONLY`。旧真实源码分析与[首合成探针](../native-tool-protocol-2026-10-08-v1/README.md)
在系统Context、工具数量、用户任务、结果长度及执行路径上同时不同，不能仅由首探针通过判断根因。
本次五工具及合成续页history保持相同，只改变一个完整产品system Context包有无；两臂均原生续页通过。

只能排除该合成包在本单次样本中必然失效的说法；不能归因旧v4、特定v5措辞、Vue正文、长历史或随机性。
不证明供应商普遍可靠，不应据此停用产品Context。没有客户源码、实际工具执行或Beta成绩。

## 2. 预注册合同与源码

冻结N及非editable安装、五个原只读Schema/alias、同一completed用户/native调用/两行截断ToolResult，
目标为`read_file`下一页和精确revision。请求构造来自原[Chat mapper](../../../src/harnessix/models/_chat_mapping.py)
及[产品Context](../../../src/harnessix/product_config/agent_context.py)，删除system后其余wire body相等。

每臂1请求，预先随机并封存顺序为产品包存在→不存在；最多2请求、无重试、180秒总期限、
每请求IO60秒、输出1024 Token、请求体32768字节上限。沿原最高档费用预留，理论两请求上界40.327680元，
不是预期费用；价格窗口、共享60元周期和新增unknown停止规则保持。输入Token精确预检缺口未修复。
两臂是独立观察，不将无目标的合法完成重试为PASS；协议、超时、价格、来源或预算失败则停止。

## 3. 离线失败、修正与实际结果

首次离线研究因Mock使用已预消费Response正文，原预算包装器未观测DONE，按`completion_incomplete`拒绝。
原失败和私有脚本语法错误保留。新根只将Mock改为未消费AsyncByteStream；生产Adapter未改。
两臂原生正控及正文标记负控共4项通过，再经42件源绑定、凭据和GET models200核验后执行。

| 臂 | HTTP | 输入/输出Token | 原生续页 | 正文 |
|---|---|---:|---|---:|
| 完整产品包存在 | 200 | 3139/140 | 正确路径、下一页及revision | 0字符 |
| 产品包不存在 | 200 | 2091/140 | 正确路径、下一页及revision | 0字符 |

两次结束均`tool_calls`，总输入5230、输出280；原Attempt/reservation逐次唯一绑定并completed。
工具输出由宿主明确合成，不执行工具。wire只保存字段类型、计数、Usage及别名摘要，不保存正文、参数或Key。

## 4. 测试与完成边界

[15项边界用例](../../../tests/models/test_chat_text_tool_boundary.py)补充预消费/未消费异步流对照，
最终Models全量646项通过；不是完整SDK或业务前后验证，不与历史644项叠加。
相同SSE字节不是传输终结符已被原预算包装器观测的证明，不能绕过原完成条件补造调用。

## 5. 费用与证据

新增估算0.025400元。周期25请求全部completed、估算0.655672元、预留0、剩余估算59.344328元；
实际账单未知。旧两笔原件不变、不计新周期。本次2请求到界即止，没有后继自动分析或追加请求。
[事实](facts.json)、[评审包](REVIEW_PACKET.md)和公开清单绑定私有25件原计划、Source Bindings、离线对照、
前置、wire元数据及账本前后结果；旧研究失败独立保留。

## 6. 后续与风险

真实长文件、任务复杂度、实际历史与供应商随机性仍须研究；不能直接复原缺失的旧wire。
真实源码覆盖、语义审阅、整改审批、受控Profile、全业务回归、浏览器及人工Beta门禁保持。
见[原生来源详设](../../changes/m09-r3-native-tool-text-boundary.md)和
[真实先导任务](../../operations/pilot-tasks/001-login-password-protection.md)。
