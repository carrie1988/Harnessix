---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, product_config, product_ui]
related_adrs:
  - docs/adr/0124-completion-guard-for-unattended-turns.md
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
related_tests:
  - tests/agent/test_completion_guard.py
  - tests/product_ui/test_rendering_host_notices.py
supersedes: []
---

# ADR 0147 交互模式下只宣布下一步的回复

- 状态：评审中
- 日期：2026-11-12
- 修订：[ADR 0124](0124-completion-guard-for-unattended-turns.md)“交互使用保持关闭”

## 背景

一步没有工具调用的回复被视为最终答复，Turn随之结束。真实终端界面的试用里两次出现：模型只说了接下来要做什么
就停下，界面显示“已完成”，实际什么也没做，用户只能再输入一次“继续”。两次的回复原文：

- “我先确认环境能力，然后写文件。”
- “No existing files in the workspace — I'll build a self-contained, dependency-free 3D black hole demo.”，后面是一份计划。

无人值守模式有完成守卫（ADR 0124）处理这类回复，交互模式一直关闭：守卫的另外两条规则（空回复或JSON、
本Turn还没有补丁）在交互使用里会误伤问答——用户问一个问题，回复本来就没有工具调用，也不需要补丁。

## 决策

1. **完成守卫增加交互模式（`interactive`）**，产品终端界面默认使用；无人值守仍用完整守卫（`full`）。
   `AgentRuntime(completion_guard=...)`接受布尔值（True为`full`）或模式名。
2. **交互模式只保留一条规则：回复宣布了下一步却没有任何动作。** 命中时宿主补发原有的
   `UNFINISHED_STEP_NOTICE`，模型在同一轮继续。
   - 判定：回复以宣布的说法开头（“我先”“接下来”“I'll”“Let me”等）或以冒号结尾，沿用完整守卫的词表；
     另外认一种写法：首句破折号之后的分句以宣布的说法开头。
   - **以问号结尾的回复不算**：那是在等用户回答，不能催模型自己往下做。
   - 上一步已经补发过就不再连续补发；每个Turn最多两次；步骤或Token预算用尽时不补发。
3. **完整守卫的判定保持原样**，无人值守与评测的行为不变。
4. **宿主补发的说明在界面上显示为一行“提示”**，不再显示成“你：[Harnessix host notice] …”的英文。
   按Item的派生身份识别（完成守卫、回复恢复两类），不看正文前缀；用户自己输入同样的文字仍显示为用户发言。

## 取舍

- 判定是词表加句式的启发式。误判的代价是多一次很短的模型请求（模型被提示后给出总结即结束）；
  漏判的代价是用户要手动“继续”。问答场景靠“问号结尾不算”和“不连续补发”两条约束降低误判。
- 没有采用“计划里还有未完成步骤就催”的规则：模型可能正是为了向用户确认才停下，催促会让它越过用户继续做。

## 后果

- 终端界面里，模型只说计划不行动时会被提示一次并继续；用户看到一行“提示：模型停下时任务还没有做完，已提示它继续”。
- 界面依赖的两类说明的Item身份函数成为产品界面与Agent之间的稳定约定。
