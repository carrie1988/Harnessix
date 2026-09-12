---
doc_type: validation-evidence
status: historical
version: 1
code_revision: 9d0be66e197a82506cf0d0dcbf59832d8865f1e2
owners:
  - core
modules:
  - evals
  - agent
related_adrs:
  - docs/adr/0044-coding-eval-contract-and-grader.md
  - docs/adr/0047-coding-eval-campaign-evidence.md
  - docs/adr/0048-controlled-real-eval-campaign-execution.md
related_tests:
  - tests/evals
  - tests/agent
supersedes: []
---

# 百炼北京Coding Eval任务v3真实质量基线

- 执行日期：2026-09-06
- 实现提交：`9d0be66e197a82506cf0d0dcbf59832d8865f1e2`
- CI：[34042784117](https://github.com/carrie1988/Harnessix/actions/runs/34042784117)，Python 3.12、Python 3.13、macOS Coding Tools和PostgreSQL全部通过
- Campaign ID：`b98a76ad-a586-4b98-aa95-fd62276380f6`
- 任务：`harnessix-openai-empty-incremental-call-id` v3
- 任务指纹：`6e7408ce04696ecdf7e72a6ea035504732e4cbdae03b663475a69b5cddd56724`
- 计划指纹：`1c3bc14f6cf9b16beb6913ad038a591e42c54fe7e01c2f82738bff2e2b4d0582`
- 模型：`qwen3-coder-plus-2025-09-23`
- 区域与协议：百炼北京、OpenAI-compatible Chat Completions
- 执行边界：3个独立run、每步骤最多1次Provider尝试、关闭并行工具、4096单次输出上限、100000 Turn累计Token预算、人民币10元试验间费用停止线

本目录只归档脱敏计划和聚合报告。私有配置、凭据环境值、供应商响应正文、Session、工作区、工具输出和账本均不进入仓库。

## 1. 归档文件

| 文件 | SHA-256 |
|---|---|
| `campaign-plan.json` | `176f181aae1459e2467860c4df663f55ba6ed98784a028c415d49e2646ec81ac` |
| `campaign-report.json` | `dc2de307f9a871e6e2059deec0b0cd5c5a0066e695230ac2148087dbb07279fb` |

两份JSON均可由严格Pydantic契约反序列化。仓库文件使用普通只读发布权限；运行时私有原件保持0600。

## 2. 聚合结果

| 指标 | 结果 |
|---|---:|
| 计划/完成试验 | 3 / 3 |
| 严格通过 | 3 |
| 预算失败 | 0 |
| 任务失败 | 0 |
| Provider失败 | 0 |
| Eval基础设施失败 | 0 |
| 一般Runtime失败 | 0 |
| 模型尝试 | 31 |
| 输入Token | 193539 |
| 输出Token | 3392 |
| 已知估算成本 | ¥0.828428 |
| 成本完整性 | complete |
| 时延min / P50 / P95 / max | 23.217640 / 23.431778 / 25.270868 / 25.270868秒 |

31次模型尝试全部为各步骤的`index=1`，实际模型身份一致，Usage完整。没有SDK自动重试、未知成本或费用停止；三次试验总成本低于人民币10元边界。

## 3. 单次结果

| run ID | 步骤/尝试 | 输入/输出Token | 时延 | 成本 | 终态 |
|---|---:|---:|---:|---:|---|
| `d904e7b1-ed3e-4ee7-9168-2921b9a8d420` | 10 / 10 | 62220 / 1104 | 25.270868秒 | ¥0.266544 | passed |
| `1cd2f85c-4d2a-40fb-a2f2-0c7a9a21b5ad` | 10 / 10 | 62198 / 1121 | 23.217640秒 | ¥0.266728 | passed |
| `72a5229c-c15e-4a23-8ed7-fd3a9ae390f0` | 11 / 11 | 69121 / 1167 | 23.431778秒 | ¥0.295156 | passed |

三个run均只修改`src/harnessix/models/_chat_stream.py`，行为检查、身份漂移回归、暂存区检查、测试反馈顺序和Git反馈顺序全部通过。最终回答均为无Markdown围栏的单个JSON对象，严格包含`summary/changed_paths/tests`，且声明路径与`focused`测试结果和实际证据一致。

## 4. 工具纠正与契约适用性

私有Session字段级审计确认三个run均形成同一序列：首次读取返回分页位置和内容revision，遗漏`expected_revision`的后续页被`tool_expected_revision_required`拒绝，随后新工具调用携带上一成功页的revision并读取成功。三次均没有SDK重试或相同工具调用重放。

任务v3相对v2只在模型可见Prompt中增加最终回答裸JSON结构，不改变历史仓库、隐藏检查、允许路径、评分器、工具、预算或模型。v2纠正后Campaign中两个代码闭环因不可见Schema而严格失败；v3在同等条件下3/3严格通过，证明公开协议消除了该测量偏差，而不是放宽评分器或事后清洗回答。

## 5. 费用边界

分页纠正后的任务v2诊断Campaign成本为¥1.255496，本次v3基线成本为¥0.828428，两者合计¥2.083924。该数字仅为版本化价格快照对完整Token Usage的估算，不替代供应商账单对账。

## 6. 结论

0.5.5c的真实Provider基线门禁关闭：请求前计划、独立run、单次尝试、真实工具/审批、严格评分、失败分类、时延、Token和成本证据均已落地。任务v3在当前固定历史缺陷上的严格成功率为3/3；该结果不能外推为任意仓库或任意任务成功率。下一切片是0.5.5d受控变更包与显式合入，且必须继续拒绝脏工作区、来源漂移和未经绑定批准的写入。
