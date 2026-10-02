---
doc_type: validation-evidence
status: current
version: 1
code_revision: 730f0846641700c4c697d7cc6ba03cbf1a8364bc
owners: [core]
modules: [agent, session, evals]
related_adrs:
  - docs/adr/0107-authenticated-eval-host-and-history-read.md
related_tests:
  - tests/session/test_authenticated_history.py
  - tests/evals/test_task_pack_publication.py
  - tests/evals/test_task_pack_evidence_stop.py
  - tests/artifacts/test_batch_diff.py
supersedes: []
---

# 完整认证历史与评测宿主：集成候选验证报告

## 1. 需求与验证对象

正式评测必须与默认产品使用同一安全和恢复合同，不能从无认证Store或不同读版本的拼接结果恢复。
本次集成验证原Session单事务完整历史Reader，以及三个Trial分支共用的原Root Owner、Key、Binding和保护Scope。
候选基于`730f0846641700c4c697d7cc6ba03cbf1a8364bc`；精确身份以
[完整437个生产源码摘要](production-source-sha256-publishable.json)和
[8个Eval源测试摘要](r3-owned-source-publishable.json)为准，不把基线提交当作未合入源码的版本证明。

## 2. 设计、调用链与源码阅读

- [架构决策ADR0107](../../adr/0107-authenticated-eval-host-and-history-read.md)：原权威、依赖及取消兼容边界。
- [Session总体与详细设计](../../changes/m09-r4-authenticated-thread-history.md)：同事务MAC、原完整前缀、字段、伪代码、资源结算及四图。
- [Eval总体与详细设计](../../changes/m09-r3-eval-publication-wiring.md)：同源Scope、原Key、完整恢复、120秒只读阶段、类/接口及四图。
- [当前Session模块](../../modules/session.md)、[Eval模块](../../modules/evals.md)和[Agent错误模块](../../modules/agent.md)。
- [正式Reader](../../../src/harnessix/session/sqlite_history.py)、[原Session门面](../../../src/harnessix/session/sqlite.py)、
  [认证宿主](../../../src/harnessix/evals/task_pack_publication.py)、[Trial编排](../../../src/harnessix/evals/task_pack_trial.py)和
  [Case前缀](../../../src/harnessix/evals/task_pack_execution.py)。

调用次序为：原Token捕获读取期限→原Owner/私有根→原Key/Binding/Scope→完整同读认证→原报告或终态核对→必要时进入原执行端口。
`authenticated_single_thread`只负责认证身份和历史，Trial只负责原业务编排；没有新的证明体系、执行能力或恢复平台。
取消的非空完成前缀拒绝绕过认证，空前缀及未取消的原停止/费用顺序保持。

## 3. 实际结果与适用范围

| 验证 | 精确范围 | 当前结果 |
|---|---|---|
| 源码集成回归 | [29个去重选择器](selectors.json)，Session21、Eval5、Git schema3；含真实SQLite、CAS/Git和原回归 | [1376通过](related-publishable.xml)，0失败/错误/跳过 |
| 全生产类型 | 当前实际437个生产`.py`，无增量缓存 | [Mypy通过](mypy-publishable.log) |
| 结构治理 | 原600/100/20阈值、全部旧热点和依赖环；仅新增批准的`evals->secrets` | [通过](readability-final.log)；Trial589行、认证宿主264行 |
| Session独立复核 | 同冻结Session正式62及原Owner/驱动私有8项 | [限定复核通过](../authenticated-thread-history-2026-10-02-v1/README.md)，不是全仓复审 |
| R3最终独立复核 | 职责拆分后136正式及8独立负例；后继纯格式变化另核精确字节及AST | [136](peer-r3-136.xml)及[8](peer-r3-private8.xml)通过；[最终等价及两项文档闭环](peer-format-doc-closure.json)确认 |
| 唯一新Wheel源码外 | 同一制品、两独立macOS ARM64 Python3.12.7/3.13.8、原29选择器 | [3.12](installed-final-312.xml)及[3.13](installed-final-313.xml)各1376通过，0失败/错误/跳过；首轮失败保留 |

前序1373通过和609行结构FAIL保留；新增3项实际SQLite空/唯一/多Thread证明职责迁移，
没有提高阈值或删减原语义断言。各测试组有重叠，不相加冒充全仓测试数。
完整XML不包含模型请求；测试使用固定合成材料，未读取钥匙串或调用模型。
后继只按原Ruff格式化3个活动文件，[完整AST等价证明](format-ast-equivalence.json)确认原逻辑与断言未减少；
上述1376项已重新在最终格式字节运行，并非由前序通过推定。前序结果保持各自输入身份。
原3份封存验证脚本采用[精确路径格式边界](archive-style-boundary.json)，不改其manifest/字节；
没有排除活动源或测试，[全树活动格式](ruff-format-final.log)及[Lint](ruff-lint-final.log)均通过。

唯一Wheel SHA256为`8d0b349ecb796c642155e5be641092b4d0ba563fc528e034be51918d6798960d`。
[独立制品复核](wheel-parent-proof.json)重新读取源码资源、实际ZIP及两安装树，全部478成员集合和字节相等；
各环境70个锁定依赖，见[依赖身份](wheel-dependency-identity-proof.json)。
首轮各22失败/2错误来自验证宿主缺少动态测试辅助模块和限定旧版夹具识别；
修正仅验证宿主，原[3.12失败](installed-first-fail-312.xml)和[3.13失败](installed-first-fail-313.xml)保留，
不重建Wheel或修改正式断言。每环境38个当前Wheel进程与2个固定旧版夹具生成进程，
见[多进程来源证明](wheel-multiprocess-source-proof.json)；后者仅执行固定旧提交创建真实旧数据。
当前候选及迁移子进程没有源码回退，不能声称所有进程都来自新Wheel。

## 4. 封存边界、公开运输与权限

前序[48成员包](../m09-r3-eval-publication-wiring-20261002-v1/REPORT.md)及
[133项49成员包](../r3-authenticated-host-integration-2026-10-02-v1/REPORT.md)保持原字节和历史结论。
其旧私有校验器要求目录0700/文件0600；Git不保存这类私有目录权限，因此不能用公开克隆的模式失败
否认内容身份，也不能将其重新标为私有权限PASS。私有模式验证、公开运输内容核对须分别记录。
当前源/测试摘要与新结果单列；不改旧manifest或把新的136项源码归属于旧133项包。
[原件引用](original-references.json)区分新结果、历史FAIL和费用账本身份。

## 5. 失败、安全与发布限制

- 原事件MAC、投影、完整重放、Owner/Key/Scope任一不成立，须在Provider或原Runtime恢复之前拒绝。
- 已有Run缺原Key/MAC不初始化或补签；返回普通历史不授当前Root、批准或执行资格。
- 新只读阶段120秒不延长Turn、工具、Token或费用预算；底层原10秒/5秒限制及取消资源结算保持。
- Reader是单库同读事实，不证明六库同时捕获；Git完整Writer/Loader、产品交付、Backup v2及新Root重批准另行完成。
- 百炼账本字节不变，新增请求0；新旧未决费用仍全额预留，不自动重试或释放。
- 真实R3完整20 Trial、Windows消费者、独立Beta及同候选R1～R6继续开放，不宣称商用发布。

Windows原生分支诊断的[固定Run36969196042](https://github.com/carrie1988/Harnessix/actions/runs/36969196042)
已终态FAIL；[低敏原件](native-preflight-result.json)及[原件身份](native-original-reference.json)
证明`PREFLIGHT_REFUSED`且未执行Git或CDB业务观察。具体准备原因尚未知，不推测或标为产品修复。
此Run绑定已发布730候选，与当前认证宿主离线测试是不同验证范围，不能互相替代。

## 6. 评审与后继

最终独立实现与文档审查、唯一制品源码外验证已完成；同一源码与测试输入冻结为集成候选。
重大源变更必须重新绑定结果；不得用旧候选通过证明后继源码。当前待验收项以[结构化结果](SUMMARY.json)为准。
