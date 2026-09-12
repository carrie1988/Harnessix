---
doc_type: source-research
status: historical
version: 1
code_revision: 9d0be66e197a82506cf0d0dcbf59832d8865f1e2
owners:
  - core
modules:
  - evals
  - models
  - agent
related_adrs:
  - docs/adr/0051-versioned-eval-final-answer-contract.md
related_tests:
  - tests/evals
supersedes: []
---

> **冻结源码研究**：本资料的参考版本与访问日期冻结于2026-09-06；具体提交、版本和证据位置见正文及[统一研究基线](baselines.md)。结论不随上游分支移动自动更新，Harnessix现行行为以关联ADR和模块设计为准。

# Coding Eval最终回答契约可见性研究

- 日期：2026-09-06
- 范围：内置历史任务Prompt、Agent模型请求、严格最终回答评分与版本兼容

## 1. 问题

分页纠正后任务v2真实Campaign中，两个run已经完成目标修改、行为/回归检查、测试反馈和Git核对，但最终回答检查失败。需要区分模型没有遵守已知协议与协议从未进入模型可见上下文，否则严格0/3不能形成公平基线。

## 2. 实现求证

### 2.1 任务Prompt

`src/harnessix/evals/catalog.py`中的v1/v2 Prompt只声明“按约定输出JSON”，没有给出字段、类型、测试Profile、是否允许Markdown围栏或示例。v2只调整累计Token预算，继承同一Prompt。

### 2.2 模型请求

`src/harnessix/agent/runtime.py`构造`ModelRequest`时，历史只包含已完成的`TextContent`、`ToolCallContent`和`ToolResultContent`。当前0.5阶段没有系统指令层，也没有Eval专用隐藏消息；Provider因此只能看到任务Prompt、工具Schema及后续工具历史。

### 2.3 评分器

`src/harnessix/evals/grader.py`直接对最终文本执行`json.loads`，再以严格模式验证`EvalFinalAnswer`。`src/harnessix/evals/contracts.py`要求三个字段：非空`summary`、排序唯一的`changed_paths`以及排序唯一的`tests[{profile, passed}]`。Markdown围栏、额外字段或不同结构均不通过。

### 2.4 真实证据

任务v2分页纠正后Campaign `0a0ee9f3-8d4d-46cb-8e38-b1956a7068d8`中，两个完成run的回答带Markdown围栏并使用自定义结构；二者的代码、测试和Git机器事实全部通过。该现象与未公开Schema直接一致，不应通过降低评分严格性掩盖。

## 3. 设计原则

1. **评分契约必须在请求前可见**：机器严格评分的字段、类型和外层格式必须写入任务版本化Prompt；
2. **评分器保持严格**：不剥离Markdown、不猜字段、不从自然语言提取路径和测试；
3. **历史任务不可变**：v1/v2 Prompt和指纹继续可恢复，新要求使用v3和新指纹；
4. **最小变更**：不提前实现0.6系统指令层，不修改Agent/Provider/Session协议；
5. **机器事实仍是权威**：回答声明必须继续与Git和测试证据精确匹配。

## 4. 方案比较

### 方案A：评分器容忍Markdown围栏

拒绝。它会把协议错误变成隐式解析规则，增加歧义，也不能解决字段结构未知。

### 方案B：Runtime注入Eval专用系统消息

暂不采用。当前Agent请求没有正式的系统/项目/用户指令优先级，引入仅供Eval使用的隐藏通道会提前侵入0.6并让生产与评测路径分叉。

### 方案C：版本化任务Prompt显式给出Schema

采用。它只改变任务契约和指纹，沿用现有请求、评分、恢复与报告链路，影响边界最小且可复现。

## 5. v3边界

v3继承v2的仓库来源、允许路径、隐藏检查、测试Profile、16步、100000累计Token、600秒及全部工具权限，只追加：

- 最终正文必须且只能是一个JSON对象；
- 禁止Markdown围栏或其他文字；
- 明确`summary`、`changed_paths`、`tests`结构；
- 示例固定任务允许路径和`focused`测试Profile。

Catalog仍按精确版本查询，省略版本返回最新v3。已有v1/v2 Campaign按持久计划恢复，不得漂移到v3。

## 6. 验收

离线验收必须证明v1/v2 Prompt和指纹不变、版本集合为1/2/3、v3预算及仓库边界不变、Prompt含完整裸JSON契约、未知版本拒绝、确定性运行与Campaign恢复仍通过。真实质量必须由全新v3 Campaign验证，不能修改既有回答或复用run。
