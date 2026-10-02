---
doc_type: validation-evidence
status: current
version: 1
code_revision: 7bbce1033925eaf758e295b3c76fc65dee446f30
owners: [core]
modules: [models, product_config, agent, evals]
related_adrs:
  - docs/adr/0008-provider-event-model.md
  - docs/adr/0054-context-planning-and-inspection.md
related_tests:
  - tests/models/test_tool_alias_identity.py
  - tests/product_config/test_coding_workflow_instructions.py
  - tests/product_config/test_agent_context.py
supersedes: []
---

# R3 原生工具身份与执行规则验证包

本包对应[总体与详细设计](../../changes/m09-r3-native-tool-invocation.md)。当前为离线实现验证，原真实20 Trial严格成功0/20、测试通过1/20，R3及商用门禁未关闭。

## 交付内容

- [facts.json](facts.json)：十项精确源码/测试输入及身份、指令和真实原Session诊断计数；不是全仓快照。
- [verification.json](verification.json)：实际RED、修正和验证边界；不会把通过的离线请求当成编码质量。
- [manifest.json](manifest.json)：本包、源码输入及原验证档案摘要。
- [review-packet.json](review-packet.json)：实现评审、原安全边界、未验收条件。
- [架构图](diagrams/architecture.png)、[调用流程](diagrams/invocation-flow.png)、[时序图](diagrams/invocation-sequence.png)、[证据数据流](diagrams/evidence-data-flow.png)：四图均已实际渲染和视觉检查；保留相邻mmd。

## 实际结果

工具别名原516项加新增115项共631通过；共享Factory、实际两Adapter及新建/重开Session共25通过。原红态和两个指令大小超限失败保留，未放宽原2751字节上限。全432源码模块显式类型检查通过。

关联组179个测试文件实际5129通过、57跳过，输入前后零漂移；原首次组的两个宿主导入范围失败保留，仅修正验证宿主对子进程的导入路径，没有修改产品、任务或断言。独立实现复审660通过，限定增量没有复现P0/P1/P2。各组存在交集，不相加为全仓测试总量，也不代替真实编码质量、原生Windows或商业验收。

同一Wheel源码外全新Python3.12.7／3.13.8安装，各4819通过、57跳过；176个原关联测试文件保持，包完整字节及RECORD通过，无运行时源码回退。两版本初轮各两个私有启动器导入范围失败保留，未修改产品、依赖、测试或新增跳过。

## 失败与恢复边界

别名只用于当前请求的目录匹配，不是持久工具身份、授权或秘密屏障。正文工具语法不执行，未知/旧别名不自动回退。旧Session、原请求账本、评分和20.77824元历史预留不改。没有新模型请求或新完整质量成绩，不宣传流程规则已强制保证任务完成。

## 复核方式

在受控源版本执行`python -m pytest tests/models/test_tool_alias_identity.py tests/product_config/test_coding_workflow_instructions.py tests/product_config/test_agent_context.py`，以及原models/agent/context/product_config/evals/delivery关联选择器。隔离任务子进程不应继承仓库tests包路径；不得改断言或以自动跳过掩盖导入串扰。重新运行只证明新执行的具体范围，不改写归档原件。
