---
doc_type: validation-evidence
status: current
version: 1
code_revision: 7bbce1033925eaf758e295b3c76fc65dee446f30
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0044-coding-eval-contract-and-grader.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_suite_execution.py
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_task_pack_profile_outcomes.py
  - tests/evals/test_provider_reverification_rebinding.py
  - tests/evals/test_provider_verification_budget.py
  - tests/evals/test_provider_verification_host.py
supersedes: []
---
# R3真实Suite结果验证包

## 结论

固定候选 `7bbce1033925eaf758e295b3c76fc65dee446f30` 的唯一新Suite已完成全部10个Case、20个Trial。**严格成功0/20，必需测试通过1/20，质量NO-GO。** 主分类为 `eval_infrastructure` 16、`budget` 3、`provider` 1；不能把16个无效Trial改写成任务失败，也不能把执行终态改写成质量通过。

全部65个新增模型请求均为 `completed`，完整usage固定价格估算为 **1.807932元**；原 **20.77824元未知费用预留**保留，未认定为实际账单。总额70元、轮次40元未改变；轮次累计估算2.027068元，剩37.972932元。

正式 `forbidden_edit` 相关判据在13个Trial中因空变更失败，不是13次越界写入。观测到的正式Git边界越界计数为0；数据破坏和语义级重复高风险effect的完整计数未由本次许可字段建立，不按零值判定。

## 范围与版本

- 真实Suite：`d058040a-2fa1-46d9-a328-9b7962f372f5`；同额度Binding：`a45f0b4f-7dd5-41b9-9e89-82d76f1c77f1`。
- 实际完成：2026-10-01 15:40:59.794758 UTC，即北京时间2026-10-01 23:40:59.794758。
- 报告日期为2026-10-02，不表示10月2日新增付费执行。
- 固定正式TaskPack：3仓库、10 Case、每Case两次新Trial，完整20个Run ID见[事实投影](facts.json)。
- 原质量阈值、单次尝试、输出上限4096、固定Engine镜像、70/40额度、历史记录均保留，无选择器或自动重试。
- 本包只覆盖已发生的7bb运行、正式结果审计、已完成深层诊断及离线首请求结构捕获，不包含后继别名或v4改善的实现及效果声明。

## 文件索引

| 文件 | 职责 |
| --- | --- |
| [总体与详细设计](../../changes/m09-r3-real-suite-result.md) | 运行边界、架构、正式字段、评分、费用、诊断及后继验证要求 |
| [facts.json](facts.json) | 白名单脱敏事实、20 Run ID、Case/Eval哈希、阈值与费用 |
| [verification.json](verification.json) | 已执行验证、既有审计引用、限制及只读辅助文件例外 |
| [manifest.json](manifest.json) | 本包文件SHA-256与证据身份，不递归包含自身哈希 |
| [review-packet.json](review-packet.json) | 复核结论、未建立项、交付范围与验收状态 |
| [architecture.mmd](architecture.mmd) | 正式执行与审计边界架构 |
| [execution-flow.mmd](execution-flow.mmd) | 费用停止线、Case顺序、证据发布与质量门禁 |
| [execution-sequence.mmd](execution-sequence.mmd) | 请求预留、执行、结算、工具事实与报告发布时序 |
| [evidence-data-flow.mmd](evidence-data-flow.mmd) | 正式报告链、费用、诊断与公开事实的数据流 |

四张图已由既有Mermaid CLI与Chrome实际渲染，并对最终PNG逐图查看验收。文字与边界完整，无文字裁切；流程图为纵向长图。图源/图片SHA、实际尺寸、渲染环境与视觉判定见verification.json；初版和中间版图片受限保留。

| 最终图片 | 实际尺寸（像素） | 视觉验收 |
| --- | --- | --- |
| [architecture.png](architecture.png) | 2496×2158 | PASS：分组与文字完整 |
| [execution-flow.png](execution-flow.png) | 1656×4080 | PASS：分支完整，纵向浏览 |
| [execution-sequence.png](execution-sequence.png) | 2658×3274 | PASS：参与者及右侧注释完整 |
| [evidence-data-flow.png](evidence-data-flow.png) | 2448×1704 | PASS：证据层次与限制完整 |

## 深层诊断摘要

20个实际Session诊断记录与固定20个Run ID一致，主数据库及诊断引用源码哈希未变化：

- 11个Trial仅1个模型步骤、0次工具调用。
- 10个Trial没有真实固定Profile检查却声明测试通过。
- 3个Trial出现留在assistant文本内的XML式工具标记，不构成对应真实工具调用。
- 3个Trial的首次补丁前没有基线检查。
- 正式观察覆盖另为：13个Trial无基线观察，16个Trial无最终观察。两组计数与“11个单步零工具”不可互换。

离线捕获证明同候选正式装配包含10个工具、v3基线/最终检查规则、63字符 `hx_` 哈希别名，未显式设置 `tool_choice`。捕获未访问模型网络、未调用Profile；它不是实际发送HTTP正文或质量证据，也未证明唯一失效根因。离线捕获的 `parallel_tool_calls:true` 与正式Suite严格配置的 `false` 不同，不能据此宣称实际请求参数全等。

## 复核入口与限制

- 公开包是有限事实投影，原始配置、Session、模型正文、工具参数、Artifact正文及凭据不发布。
- 原Suite报告SHA-256：`419a000c89511fa77ebba6fd0930105e3f803dbb98f9d34039cfe227b4618978`。
- 原[2026-09-20正式0/20结果](../provider-engineering-2026-09-20-v1/suite-report.json)保留，SHA-256为 `232cd80fa7412b886b60ab7ba632fbc321c84d672c9bbb41af150585c4dfab6f`；不合并新旧样本或制造因果改善。
- 正式评分与费用审计已使用固定候选的原读器、严格合同及纯汇总函数，检查实际导入来源。源码875份、正式输入90份前后SHA一致。
- 补充审计首次使用原SQLite只读辅助函数时产生0字节WAL和32768字节SHM辅助文件，已停止该读取路径、未清理；原主数据库、源码、账本和正式报告内容未改写。后续采用冷数据库不可变只读连接，详见[验证记录](verification.json)。
- 本包没有执行Suite、注册、账本Owner进入、模型、Docker、CI、Git或网络；未独立复算完整原始Transcript哈希。
- 后继候选需支持不可变历史、原额度累计和显式管理绑定。当前单次切换合同不支持第二次切换，后继管理契约尚未实现；这是管理能力缺口，不是70/40额度不足。本包不执行后继登记、恢复或模型请求。质量结论不构成商用1.0或平台发布验收。
