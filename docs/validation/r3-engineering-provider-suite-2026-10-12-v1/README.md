---
doc_type: validation-evidence
status: current
version: 1
code_revision: c6c58b690095bb5be493144a8f9c89f13b4b74f3
owners: [core]
modules: [evals, models, sandbox]
related_adrs:
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
related_tests:
  - tests/evals/test_provider_suite_evidence.py
  - tests/evals/test_provider_suite_contracts.py
  - tests/evals/test_suite.py
supersedes:
  - docs/validation/provider-engineering-2026-09-20-v1/README.md
---

# R3真实Provider工程Suite验收

## 结论

Revision `c6c58b690095bb5be493144a8f9c89f13b4b74f3`上的独立真实Provider Suite已完成并通过R3：

| 指标 | 结果 |
| --- | ---: |
| 仓库 / Case / Trial | 3 / 10 / 20 |
| 严格任务通过 | **20/20** |
| 必需测试通过 | **20/20** |
| 人工干预 | **0/20** |
| Provider失败 / 成本不完整 | 0 / 0 |
| 模型请求步骤 | 152 |
| 输入 / 输出Token | 558,728 / 13,800 |
| Suite已知成本 | CNY 7.201536 |
| 最大单Trial步骤 / Token / 时长 | 10 / 49,574 / 244.183755秒 |

本次仍使用原`harnessix-engineering/v2`、10 Case×2 Trial、`qwen3.8-max`、non-thinking、
`max_attempts=1`。每个Task保持16 Step、50,000累计Token、900秒、65,536字符输出和每Step最多8次工具调用；
Task Pack、允许修改路径、固定Container Profile、Grader、权限与失败语义均未放宽。

## 固定身份

| 项目 | 值 |
| --- | --- |
| Suite ID | `f930752e-a55e-4075-b97d-6cc1de9dba5d` |
| Plan Fingerprint | `6d18ea495160782f9cf9dd00b0b814cf2b8cfd43197c45a6df358dc4608b0b50` |
| Report SHA-256 | `72881f59688eb2a0b1fa6eb23bcc6138a4b90cb621a2a09ba1c92677ba240d0a` |
| Task Pack SHA-256 | `6ab16bf0a305c5d5a590be02f028184a9510e941146f3b8d27a3ef99ee7b7c46` |
| Provider / 地域 | `openai_chat` / `cn-beijing` |
| 完成时间 | 2026-10-11T16:46:38.488954Z |

正式合同重读审计确认：10个Case与20个Run ID均唯一；每Case恰好2个Trial且全部通过；全部Turn正常完成、
测试检查全通过、成本完整、无Provider/Agent/预算失败；每Trial实际模型、步骤、Token和时限均在原边界内。
Suite State中的报告摘要与公开报告逐字节重算一致。

## 公开证据

| 文件 | SHA-256 | 说明 |
| --- | --- | --- |
| [`suite-plan.json`](suite-plan.json) | `5b5dfe5d93c5471da2181859c2b6d06c12e6bed8e73a6d09c9be834ecb008a6d` | 固定Suite、Case、环境与计价计划 |
| [`suite-report.json`](suite-report.json) | `72881f59688eb2a0b1fa6eb23bcc6138a4b90cb621a2a09ba1c92677ba240d0a` | 20 Trial低敏、可重算正式报告 |
| [`evidence-manifest.json`](evidence-manifest.json) | `73f7a38ef14a60463eaca0dd24233c87204304c3758deb29ef62592773a5d84e` | Pack、Revision、模型、Token、成本与摘要索引 |

公开目录不包含API Key、宿主路径、Prompt/Response正文、工具输出、Workspace、Session数据库或私有账本。
历史失败和中止Suite继续保留，不与本次20/20拼接，也不退款其未知请求预留。

## 适用边界

该证据关闭R3真实编码质量门禁，证明固定十项任务在本次模型、版本和环境下完成编码、测试与证据闭环。
它不证明R4 macOS发行、原生Git Writer/SHM、R5独立用户Beta或整个1.0商用发布已经完成。
