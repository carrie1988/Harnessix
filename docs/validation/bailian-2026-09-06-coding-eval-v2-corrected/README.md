# 百炼北京Coding Eval任务v2分页纠正后真实基线

- 执行日期：2026-09-06
- 实现提交：`7e58c15a4be9780de5837c9706f986a8613b1420`
- CI：[34039563970](https://github.com/carrie1988/Harnessix/actions/runs/34039563970)，Python 3.12、Python 3.13、macOS Coding Tools和PostgreSQL全部通过
- Campaign ID：`0a0ee9f3-8d4d-46cb-8e38-b1956a7068d8`
- 任务：`harnessix-openai-empty-incremental-call-id` v2
- 任务指纹：`011268310f3aac1b3025d643aaf8361b807bd9d9b55a19436caa05507d122bfb`
- 计划指纹：`88300d00e9919f3cb33d045391c4aedd44be2964744619d4f7a2d1122154cebe`
- 模型：`qwen3-coder-plus-2025-09-23`
- 区域与协议：百炼北京、OpenAI-compatible Chat Completions
- 执行边界：3个独立run、每步骤最多1次Provider尝试、关闭并行工具、4096单次输出上限、100000 Turn累计Token预算、人民币10元试验间费用停止线

本目录只归档脱敏计划和聚合报告。私有配置、凭据环境值、供应商响应正文、Session、工作区、工具输出和账本均不进入仓库。

## 1. 归档文件

| 文件 | SHA-256 |
|---|---|
| `campaign-plan.json` | `001de3170bf1cc76b18c11b90cbebd4a715263b68d4eca229e6579af19a29f34` |
| `campaign-report.json` | `95b31d1b77ab233afc66a7d68a10db8029567bd277cfd0b57b9c48fc129c8740` |

两份JSON均可由严格Pydantic契约反序列化。仓库文件使用普通只读发布权限；运行时私有原件保持0600。

## 2. 聚合结果

| 指标 | 结果 |
|---|---:|
| 计划/完成试验 | 3 / 3 |
| 严格通过 | 0 |
| 预算失败 | 1 |
| 任务失败 | 2 |
| Provider失败 | 0 |
| Eval基础设施失败 | 0 |
| 一般Runtime失败 | 0 |
| 模型尝试 | 38 |
| 输入Token | 294662 |
| 输出Token | 4803 |
| 已知估算成本 | ¥1.255496 |
| 成本完整性 | complete |
| 时延min / P50 / P95 / max | 28.148674 / 29.680875 / 30.645999 / 30.645999秒 |

38次模型尝试全部为各步骤的`index=1`，实际模型身份一致，Usage完整。没有SDK自动重试、未知成本或费用停止；三次试验总成本低于人民币10元边界。

## 3. 单次结果

| run ID | 步骤/尝试 | 输入/输出Token | 时延 | 成本 | 终态 |
|---|---:|---:|---:|---:|---|
| `49bfa8c7-3463-4993-bf59-1dcac7cc1a89` | 14 / 14 | 116905 / 1425 | 30.645999秒 | ¥0.49042 | budget |
| `8cca3113-f2b8-48d7-8d09-e376b79eb10c` | 12 / 12 | 88936 / 1766 | 29.680875秒 | ¥0.384 | task/final_answer |
| `18b1f7fc-9815-41e8-bdab-a7759b7df637` | 12 / 12 | 88821 / 1612 | 28.148674秒 | ¥0.381076 | task/final_answer |

后两个run均只修改允许文件，行为检查与身份回归通过，暂存区保持干净，并在通过测试后读取Git状态和差异。第一个run在前期错误探索后于Patch调用响应记账时超过100000累计Token，Runtime在执行Patch前停止，工作区保持无变更。

## 4. 分页纠正验证

三个run均形成相同的可审计序列：

1. `read_file`后续页遗漏`expected_revision`；
2. Runtime返回`tool_expected_revision_required`；
3. 下一次调用显式携带上一成功页的`revision`；
4. 后续页读取成功。

因此0.5.5c3c的专用错误被真实模型3/3采用，c3b中连续重复无效分页的已知交互缺陷关闭。该结论来自私有Session事件的字段级审计；归档报告不保存路径内容、参数值或模型正文。

## 5. 新暴露的评测契约缺口

两个成功完成代码闭环的run最终回答均可解析出语义内容，但不满足评分器要求：正文带Markdown围栏，且字段不是`summary/changed_paths/tests`。评分器的严格裸JSON约束合理，但任务v2 Prompt只要求“按约定输出JSON”；`AgentRuntime`实际请求中没有独立系统指令或该评分契约，模型无法从可见上下文确定目标Schema。

因此严格结果0/3不能作为公平的端到端成功率：可确认的代码修复成功率为2/3，严格协议成功率为0/3。后续必须版本化任务Prompt，显式给出裸JSON结构和禁止围栏要求，再创建独立Campaign。不得放宽评分器、剥离Markdown围栏或事后改写本报告。

## 6. 结论

本次完成分页纠正真实适用性验证并形成新的诊断基线，但没有关闭0.5.5c整体门禁。后续任务v3保持仓库、检查、权限、100000预算和评分器不变，仅补齐模型可见最终回答契约；新Campaign不得复用本目录的run、状态或报告。
