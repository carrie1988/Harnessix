---
doc_type: validation-evidence
status: current
version: 1
code_revision: 3ebf0a37d47ce54ddd64d2e90c3f72fc3b9ccd7d
owners:
  - core
modules:
  - trusted_actions
  - agent
  - documentation
related_adrs:
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/trusted_actions/test_gateway_error_boundaries.py
  - tests/trusted_actions/test_gateway_error_runtime.py
  - tests/trusted_actions/test_operation_error_runtime.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# Gateway回调公开错误专项验证与评审报告

## 1. 验证对象与结论

本报告验证`3ebf0a37d47ce54ddd64d2e90c3f72fc3b9ccd7d`的Context Factory、审批Review、
终态Output回调**抛异常边界**，以及读写Execute/Reconcile异常的真实Runtime传播。
实现与完整设计见[专项详设](../../changes/m09-4a-gateway-callback-error-boundary.md)。

受控全量回归**4113 passed、32 skipped，362.49秒**；新增57项专项、153项相关回归均通过。
没有真实模型请求，没有数据库Schema/Fingerprint迁移，没有重新引入HTTP/Worker服务。
阶段错误合同只决定公开分类与固定消息，不修改已成功执行的Audit事实或诱导重新执行。

**结论：窄回调异常候选通过本地验证；发布仍阻断，整体0.9与0.9.4a均未完成。**
结构化失败Outcome开放缺口已实际复现，许可证12件Archive仍被原策略拒绝，其他门禁独立开放。

## 2. Manifest、评审包与验证资料

| 资料 | 内容与用途 |
|---|---|
| [verification.json](verification.json) | 实际命令、计数、摘要输出、负例重放、静态质量、时间与范围排除；不同测试组重叠，不可相加。 |
| [gateway-facts.json](gateway-facts.json) | 三阶段完整固定码表/消息、57项覆盖、无Schema迁移事实，以及结构化结果缺口的五面布尔证据。 |
| [ci-observation.json](ci-observation.json) | 前序6677e54的真实CI终态与Windows作业事实；明确不是新Gateway代码的CI验收。 |
| [review-packet.json](review-packet.json) | `release_blocked`决定、适用范围、限制与全部开放门禁。 |
| [bundle-manifest.json](bundle-manifest.json) | 五份资料的原字节SHA-256/大小及16个精确Git源码输入；Manifest自身排除。 |

资料集中于本目录，不修改[前序许可冻结包](../license-evidence-2026-09-27-v1/README.md)的历史状态。
原始故障诊断不作为公开资料保存；合成缺口证据仅保存出现/未出现的布尔事实，不保存诊断正文。

## 3. 验证步骤与源码关系

1. 在修复前注入16项异常负例，全部失败；8项原取消对照通过。
2. 从精确6677e54 Git归档独立重放同一16项负例，生产源码保持原字节；只移除旧版本不存在且被
   deselect的码表测试导入，不更改负例断言。结果仍为16 failed，证明并非仅测试错误对象。
3. 修复后新增专项57 passed，2.10秒：33项直接/取消、15项真实Runtime回调、9项执行/对账。
4. 相关组153 passed，4.24秒；覆盖Gateway、Router、Agent审批/恢复及产品Process与配置。
5. 完整受控命令为`uv run pytest --ignore=tests/security -q -o addopts=''`。
   未跟踪的攻击草稿明确排除，不能作为TM-01～TM-13已验收的证据。
6. Ruff检查767份Python文件，Mypy检查328份源码；可读性报告、合同、TaskPack、SBOM Schema/漂移、
   Secret六规则自检及文档门禁通过，使用安装的Chrome实际渲染变更Mermaid。
7. 冻结包新增1个Manifest回归参数后另行执行治理/文档回归；该参数不在4113全量计数内；冻结后治理/专项组合287 passed、21.47秒。

可读性治理保持600行阈值，Gateway核心通过迁移纯审批投影而收敛，不加入超大文件豁免。
Full、专项、相关与冻结后治理测试存在包含关系，不能累计成更大的独立总数。
本报告不声称`make check`通过：许可证门禁仍以exit 1拒绝12件Archive。

## 4. 失败、取消与恢复结果

- Context回调失败不保存Route、不执行；Review失败保持待批准计划，没有批准检查点。
- Output失败保持原Audit终态和摘要，Executor调用数保持1，确定终态不盲对账。
- 写动作投影持续失败时，Session的UNKNOWN投影和Turn的INTERRUPTED/uncertain_effect守卫
  与Audit的succeeded可以同时存在，各自表达效果事实和交互完成情况，不能伪造一致。
- `CancelledError`、`TurnCancelled`保持传播；异步Task/Token取消以进入/退出Event握手确认
  子任务已经回收，不使用固定sleep猜测请求是否进入。
- 只读Executor故障真实进入第二次ModelRequest历史；写UNKNOWN停止模型，不自动重复执行。
- Reconcile测试在Router已经保存UNKNOWN、Session尚未记Result的故障点恢复，只对账一次。

## 5. 前序CI实际终态与版本边界

[CI 36291475364](https://github.com/carrie1988/Harnessix/actions/runs/36291475364)精确对应
`6677e549e4883704856dbf55162b00b2ff7291b3`：四作业成功、一失败、一取消。

- Windows：Benchmark 225 passed/229.09秒；选定治理/运行时组723 passed、48 skipped/254.12秒。
  实际Wheel/sdist构建通过，Secret扫描2232输入完整、六固定规则零命中。
- macOS Coding Tools、documentation、container-sandbox作业成功。
- Python 3.12在`license_scan.py --check`因12件Archive违规exit 1；3.13由矩阵取消。

这些事实验证前序Windows夹具修复，不证明新Gateway代码跨平台验收或完整CI成功。
新候选在冻结时尚未启动CI；后续后台结果须按精确提交另存，不覆盖本报告。

## 6. 结构化Outcome已确认开放缺口

使用只读Fake Executor直接返回格式合法的未登记错误码及合成诊断对象，经过真实Runtime、
SQLite和SDK事件回放；确实产生2次Scripted Provider请求、1次Executor、0次Reconcile。

| 公开面 | 未登记码出现 | 合成诊断正文出现 |
|---|---|---|
| Model历史 | 是 | 是 |
| Session | 是 | 是 |
| Audit | 是 | 否，只存摘要 |
| Protocol事件回放 | 是 | 是 |
| 非空Span/Metrics | 否 | 否 |

Thread快照与事件回放具有不同投影范围，不能只检查简化快照就宣称Protocol安全。
本专项清洗抛出的异常，不覆盖上述正常返回对象。后续需制定宿主有限失败码、失败正文公开合同和
Process/Eval合法诊断保留规则，验证摘要、恢复与持久计划兼容。不可直接删除所有失败输出掩盖缺口。

## 7. 开放门禁与发布决定

剩余包括结构化Outcome/Provider返回边界、受限许可证处置与商业权利链、不可变安装/镜像输入、
编号攻击回归、远端MCP身份/OAuth/受管出口与故障恢复、真实三平台安装/升级/卸载及受控Beta、
真实Provider发布验证。没有真实API调用或新增定时任务，历史证据保持原字节。

检查回调异常的57项绿色结果不替代这些门禁；项目继续以[路线图](../../roadmap.md)为总验收依据。
