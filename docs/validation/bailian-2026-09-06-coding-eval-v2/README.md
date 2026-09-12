---
doc_type: validation-evidence
status: historical
version: 1
code_revision: 397542942be8474d99feb190a901e8b336a19bdd
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

# 百炼北京Coding Eval任务v2三次真实基线

- 执行日期：2026-09-06
- 实现提交：`397542942be8474d99feb190a901e8b336a19bdd`
- CI：[34036786555](https://github.com/carrie1988/Harnessix/actions/runs/34036786555)，Python 3.12、Python 3.13、macOS Coding Tools和PostgreSQL全部通过
- Campaign ID：`ee2ccba3-20da-46c0-9e99-8b8597a461c3`
- 任务：`harnessix-openai-empty-incremental-call-id` v2
- 任务指纹：`011268310f3aac1b3025d643aaf8361b807bd9d9b55a19436caa05507d122bfb`
- 计划指纹：`15bcbd64fb285c74e74785eb4b0c6ea7a73b675528467c796051c7337b867f93`
- 模型：`qwen3-coder-plus-2025-09-23`
- 区域与协议：百炼北京、OpenAI-compatible Chat Completions
- 执行边界：3个独立run、每步骤最多1次Provider尝试、关闭并行工具、4096单次输出上限、100000 Turn累计Token预算、人民币10元试验间费用停止线

本目录只归档脱敏计划和聚合报告。私有配置、API Key环境值、供应商响应正文、Session、工作区、工具输出和账本均不进入仓库。

## 1. 归档文件

| 文件 | SHA-256 |
|---|---|
| `campaign-plan.json` | `0420e067e4ca8586b0a903d960f3d08c65d53f8ab0c464ec6d3f8c3dbcfc6a1b` |
| `campaign-report.json` | `545c95ab81669c6a31522a9baea4d489358f89a5e3cfaaea6ff4c0d80da94ca3` |

两份JSON均可由当前严格Pydantic契约反序列化，并能从单次证据重算聚合字段。仓库文件使用普通只读发布权限；运行时私有原件保持0600。

## 2. 聚合结果

| 指标 | 结果 |
|---|---:|
| 计划/完成试验 | 3 / 3 |
| 通过 | 0 |
| 预算失败 | 3 |
| Provider失败 | 0 |
| Eval基础设施失败 | 0 |
| 一般Runtime失败 | 0 |
| 任务失败 | 0 |
| 模型尝试 | 41 |
| 输入Token | 324142 |
| 输出Token | 3717 |
| 已知估算成本 | ¥1.35604 |
| 成本完整性 | complete |
| 时延min / P50 / P95 / max | 25.965035 / 26.084084 / 27.332026 / 27.332026秒 |

41次模型尝试全部为各步骤的`index=1`，实际模型身份一致，Usage完整。没有SDK自动重试、未知成本或费用停止；三次试验总成本低于人民币10元授权边界。

## 3. 单次结果

| run ID | 步骤/尝试 | 输入/输出Token | 时延 | 成本 | 终态 |
|---|---:|---:|---:|---:|---|
| `d23dc017-4ac1-4a45-b7b3-3658fc660758` | 14 / 14 | 106384 / 1180 | 27.332026秒 | ¥0.444416 | budget |
| `4a7e0fe9-f0a3-46c3-892e-3bdc67f059b8` | 13 / 13 | 113681 / 1179 | 26.084084秒 | ¥0.473588 | budget |
| `8b782079-9cd5-4510-a509-d0a7eeec1f49` | 14 / 14 | 104077 / 1358 | 25.965035秒 | ¥0.438036 | budget |

三个Turn最终实际累计Token分别为107564、114860和105435，均在Provider终态记账后超过100000，随后禁止继续调度工具。三个工作区均无变更、无最终结构化回答；行为检查失败、身份回归检查通过。

## 4. 行为序列与根因

v2已把首轮v1在第5步、读取目标文件之前的错误截断推迟到第13—14步。三个模型均完成初始失败测试和Artifact读取，其中两个读取了目标`_chat_stream.py`首页，另一个读取了相关测试文件首页。

文件首页结果包含`revision`和后续页位置。模型随后使用`start_line`/`max_lines`请求后续页，但没有携带上一成功结果的`expected_revision`。该条件由Pydantic模型验证器执行，JSON Schema不能直接表达；Harnessix只返回通用`tool_invalid_arguments / 工具参数不符合契约`，没有指出缺少哪个跨字段参数。

观察到的无效后续页调用：

- `d23dc017…`：连续3次缺少`expected_revision`，随后改用搜索，但未在预算内回到修复闭环；
- `4a7e0fe9…`：连续4次缺少`expected_revision`，第5次同类请求在Provider记账后触发预算门禁；
- `8b782079…`：连续8次缺少`expected_revision`，第9次同类请求在Provider记账后触发预算门禁。

每次失败后的新模型步骤都会重新发送增长后的完整历史和工具Schema，因此累计输入迅速扩大。第二个run最后两次请求各产生约19700输入Token。终态主分类虽然是`budget`，但可操作根因是**工具参数错误反馈不可自纠正**；继续单纯提高Token预算只会扩大费用，不能修复交互协议。

## 5. 结论

本次完成了任务v2和100000预算的真实适用性验证，但仍未形成可用于模型编码成功率的有效基线：0/3不能解释为模型不会修复缺陷，也不能再解释为原20000预算过小。它证明了下一层正式缺口——严格工具契约必须向模型返回有限、可操作且不泄漏数据的校正提示。

后续顺序调整为：

1. 源码求证主流Agent如何把参数校验错误反馈给模型；
2. 为`read_file`/`list_files`分页缺少revision提供稳定、无参数回显的校正错误；
3. 补直接工具、Agent循环、恢复、隐私和回归测试；
4. 在新的固定预算与执行边界下创建Campaign，验证模型能否从一次错误中纠正；
5. 成功形成有效质量基线后，才进入0.5.5d变更交付。

不得修改本Campaign的计划、状态、run ID或报告，也不得把后续试验追加到本报告。
