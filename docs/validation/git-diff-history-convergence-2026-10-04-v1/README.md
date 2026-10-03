---
doc_type: validation-evidence
status: current
version: 1
code_revision: f84583e5560998683cf07d6b4c138c4f7ff3ad1a
owners: [core]
modules: [tools, evals, documentation]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_git.py
  - tests/tools/test_git_delivery_reader.py
  - tests/tools/test_git_platform_contracts.py
  - tests/tools/test_windows_git.py
  - tests/evals/test_git_evidence.py
  - tests/evals/test_delivery.py
  - tests/governance/test_windows_git_first_failure_projection.py
  - tests/governance/test_windows_git_trace2_input_binding.py
supersedes: []
---

# Git Diff完整对象身份与历史输入断言收敛验证报告

## 1. 验证目标、基线与结论

基于`f84583e5560998683cf07d6b4c138c4f7ff3ad1a`，分别修复跨副本Diff证据身份漂移和三个历史输入断言。
原干净受管源码完整回归11260项中11119通过、4失败、137跳过；全部原失败保留，不将四个失败统称为测试资料问题。

最终六个Git Tool／Evidence／Delivery原关联文件158项中129通过、29平台跳过、零失败或错误；
两个历史Contract原文件173项全部通过。两集合互不重叠；不把重复中间回归累加为独立覆盖数量。
最终八文件联合执行331项中302通过、29平台跳过；独立审查相同五件源码SHA的六个文件284项全部通过，
确认能力策略缺口闭环，无未解决限定发现。审查六文件包含历史173项，不与联合成绩重复累加。
这些结果证明本地限定整改，不代表全仓后继、Windows原生核心编码、真实R3质量或商用发布完成。

## 2. 两类根因与真实行为核验

### 2.1 已批准交付的完整Diff摘要

两个实际仓库的目标文件均9594字节、模式0644、完整SHA-256相同；原Diff分别708与710字节，
index行对象缩写分别7位和8位，完整正文摘要因此不同。追加`--full-index`后，两份完整正文摘要一致。
修复位于原[`_execute_git_read`](../../../src/harnessix/tools/git.py)，Eval继续复用原端口，
不忽略index行、不只比较Hunk、不替换预期摘要、不修改Grader。

新增真实仓库对照覆盖SHA-1／SHA-256、worktree／staged及7／8位缩写；完整对象身份与实际
`rev-parse`和`hash-object`返回值精确相等，不硬编码40位。原迟到Workspace漂移仍拒绝。

`GitReadRuntime.contract.full_index=true`参与既有工具能力摘要。新增两项旧能力请求先失败；
策略绑定后旧请求以`tool_contract_changed`拒绝，新请求正常成功。公开输入／输出Schema不增加字段，
旧Eval Report和Package不重签或重评分，也不声称旧Run可跨候选恢复。

### 2.2 历史切片与当前输入身份

历史两件追加固定比较`5306c71`→`9b9e52f`，首失败四叶固定比较`f07263c`→`0b1e16a`；
固定历史源码Blob构成普通临时树，旧前17成员精确接受、完整旧18成员因真实末成员改变拒绝。
当前目录与`0583b53306f3ab869fb35c5b9eece80fbe2251a4`固定目录独立类型敏感比较，
当前真实18个源文件继续执行原`source_checks`。

生产检查器、固定目录、PE/PDB、原27选择器、原13Hook与20／45／240／300预算均未改。
原12项等值异类型、未知字节、LF／CRLF及表示配对负例仍执行；不增加宽松允许集合。

## 3. 测试范围、原失败与最终成绩

| 阶段 | 实际结果 | 证明与边界 |
|---|---|---|
| 原完整干净回归 | 11119通过、4失败、137跳过 | 本次归因基线；不改写为后继成绩 |
| 新缩写宽度反例 | 2失败 | 原命令产生不同完整正文；红灯保留 |
| 最初三个关联文件 | 26通过 | 实际批准交付与迟到漂移检查通过 |
| 六文件首次扩展 | 117通过、8失败、29跳过 | 八项完整argv断言尚未同步新增固定参数 |
| SHA算法／argv同步后 | 127通过、29跳过 | 原八种参数组合全部精确比较，不删guard |
| 固定策略能力反例 | 2失败 | 仅改变argv不改变原工具版本的缺口 |
| 最终同六文件 | 129通过、29跳过 | 策略绑定、旧请求拒绝及真实SHA算法全部覆盖 |
| 历史两个原文件 | 173通过 | 历史固定差分与当前严格输入同时验真 |
| 原Mypy全生产范围 | 438件源码通过 | 类型检查，不是工程任务质量成绩 |

29项跳过来自本机非Windows原生环境；不得据此宣告Windows业务执行已验证。
pytest使用Python3.12.7、Git2.53.0、独立临时根、原异步插件并关闭cacheprovider；没有模型请求。
原Ruff行宽及可读性报告漂移失败单独保留，后继按原格式及生成器同步，未修改可读性policy或增加例外。

## 4. 设计、源码与结构化证据

- [Git Diff总体与详细设计](../../changes/m09-r4-git-diff-full-object-identity.md)：架构、流程／时序／数据图、接口、字段、伪代码和失败兼容边界。
- [历史输入详设第15节](../../changes/m09-r4-windows-trace2-role-input-binding.md#15-历史精确差分与当前发行输入的独立验证)：固定候选、现行守卫及原拒绝合同。
- [Tools现行模块设计](../../modules/tools.md)和[Eval现行模块设计](../../modules/evals.md)：实际装配与证据比较。
- [真实Git对照](../../../tests/tools/test_git.py)、[完整参数与Contract守卫](../../../tests/tools/test_git_delivery_reader.py)。
- [首失败历史测试](../../../tests/governance/test_windows_git_first_failure_projection.py)、[两成员历史及当前输入测试](../../../tests/governance/test_windows_git_trace2_input_binding.py)。
- [结构化结果](result.json)与[原件摘要](SHA256SUMS)：最终源文件SHA、各阶段计数、政策身份、独立新构建Wheel身份及未验证边界。

私有交付资料保留实际命令、完整JUnit、红灯、源摘要、设计实现前摘要、六幅已渲染设计图和审查记录。
公共摘要不包含凭据、用户Workspace正文、原始CI业务日志或调试日志。

## 5. 发行、恢复与未完成项

本次独立构建Wheel只证明当前源可生成发行物，不能自动继承较早beda980的三平台安装生命周期成绩。
后继固定候选须独立取得常规整合与必要原生结果；旧失败Run不以原样重跑覆盖。
不增中间件、重试、诊断协议或迁移工具。完整Git／Backup v2、真实R3及费用未决、消费者平台、
独立Beta、权利和R1～R6仍开放，`commercial_release=false`。
