---
doc_type: validation-evidence
status: current
version: 1
code_revision: 7bbce1033925eaf758e295b3c76fc65dee446f30
owners: [core]
modules: [delivery, product_config, processes]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_material_trace2_contracts.py
  - tests/product_config/test_git_material_trace2_binding.py
  - tests/product_config/test_git_trace2_success_observation.py
  - tests/governance/test_git_trace2_projection.py
supersedes: []
---

# Windows Git Trace2 生产诊断评审材料

## 1. 评审对象及输入身份

- 主设计：[m09-r4-git-material-trace2.md](../../changes/m09-r4-git-material-trace2.md)。
- 当前HEAD：`7bbce1033925eaf758e295b3c76fc65dee446f30`；以19件实际输入身份`7da875ba1e84f97d9647b5d0bd3be2cbb826088692e5640c4ec92fe4647a1d07`为限域候选识别依据，不是全仓验真。
- 规范profile：`7120ca7cc6d73275bd29980b3b7f66be5ff0608d02ec1b241b86505677ea6e48`；实际30种schema及6种FMT_ID。
- 输入/交付清单：[manifest.json](manifest.json)；事实与实际核查：[facts.json](facts.json)、[verification.json](verification.json)。

## 2. 当前评审决策

**设计资料可作为限域评审输入；不得据此批准Windows正式启用、宣布Git128修复或关闭商用门槛。**
成功接口本地最小修复及confirmed 311PASS已出现；新独立闭环审查已终结，状态为`P1_resolved_at_final_reviewed_bytes`，最终569PASS。
原复核包status为`completed_with_one_reproduced_open_P1`，原P1/1RED及后继两轮失败原件保留；历史包不得改写为零P1；新闭环仅在最终受审范围无P0/P1/P2，不是全仓结论。

## 3. T2-PEER-01历史、修复及回归边界

| 环节 | 可复核事实 |
|---|---|
| 原缺陷 | `_success`访问`result.proof`；实际`GitProcessCompletion`字段为`input_proof` |
| 原unit盲区 | `SimpleNamespace`提供不存在的proof字段，掩盖真实返回合同差异 |
| 原影响 | `_safe`标incomplete；completion_authenticated已置位，成功不post补取；显式原exit0被session门闩收紧为非0，业务结果未改 |
| 当前最小修复 | 读取原input_proof；None拒绝，由原_safe标不完整；无新增await、读取、重试或后验 |
| 当前unit | 改用实际Completion形状，仍为合成流合同检查 |
| 新实际用例 | `test_git_trace2_success_observation.py`使用真实Supervisor/Owner/port.run、原认证receipt/proof/PID、同一stderr内存和原proof回码 |
| 原real-completion-red | 2FAIL，原件保留 |
| 中间real-completion-green | 实际310PASS/1FAIL，不能写成311PASS，原件保留 |
| 中间失败根因 | 新测试错误把本机Git2.53要求为固定Windows2.55 profile的KNOWN；生产MISMATCH与incomplete规则正确 |
| 最终confirmed | 311PASS；断言无post尝试、reason非RAW_UNAVAILABLE、incomplete等于非KNOWN，未放宽profile或原生KNOWN强门槛 |
| 独立闭环 | `windows-git-trace2-peer-closure-20261002-v1`已终结；最终受审字节P1解决，569PASS |

新实际用例是本机成功接口证明，不是目标Windowsprofile命中、完整v5载体、Git128根因修复或业务fullCommit证明。

## 4. 必须逐项核查的设计不变量

1. 默认off：原环境与v1摘要/wire精确字段保持；实际implementation digest变化不得误称旧批准仍有效。
2. 显式mode：仅material_write、唯一profileSHA、固定GIT_TRACE2_EVENT=2及原22项argv；一般命令/读取off。
3. 原审批：Plan/checkpoint实际类型重建，强REQUIRE_APPROVAL与exactapproval先于执行task、原Owner及正文stage。
4. 原执行：无新增诊断fd、文件、Git命令；原20秒默认命令/45秒载体操作/5分钟workflow保护、13hooks及2selectors不变。
5. 成功观察：只用原Completion已验真stderr及input_proof；缺proof由_safe标不完整；无postIO。
6. 失败观察：只复用原_post_once；MAC、双raw长度/SHA/EOF及protection全部通过后才投影。
7. 纯内存合同：1MiB raw、64KiB frame、64候选event、深度16、实际类型、重复键/非有限numeric拒绝；未知/sourceprofile不匹配为UNKNOWN。
8. 输出：七字段固定投影，无SID/argv/time/msg/动态hash；原外围字段保留范围须单独审查。
9. finish：version/start/至少一个code，不强制exit与atexit都存在，不要求三见证齐全；未提供实际int回码可能KNOWN但返回一致性UNAVAILABLE。
10. 来源：4件归档C源码SHA证明静态格式，不是运行binary attestation；6种FMT来源非穷举、非唯一caller、非唯一根因。
11. sessionfinish：仅显式mode、原0且诊断不完整收紧为非0，不把业务失败变PASS。
12. 历史失败：旧Run36862927636的Git128/worker2/proof absent/readback未到达保持FAIL，当前根因未知。

## 5. 验证证据与限制

生产绑定档案196PASS（74新/122旧）、原例新mode重执行2PASS、mypy7源码、ruff/format11文件分别登记，不合并成全仓覆盖。
历史纯投影310PASS不是当前成功接口全覆盖证明；confirmed 311PASS与两轮失败逐件登记原字节SHA。
本次实际验证仅为只读源/档案核对及内存规则检查；不重复计为pytest/mypy/ruff/Windows/CI执行。

19件实际代码/测试/工作流输入数组无重复路径；闭环复核20件另含`pyproject.toml`，其最终身份`a965313ab1cf1ce8b973e04e6419c4eafe3ef1834117848e2393fa8181cd0d6c`，19件SHA逐件对应，不是全仓。既有CAS用例、Supervisor与Owner依赖不纳入该限域集；未复制私有档案、原stderr正文或个人路径。
本交付只修改主设计与本交付目录；不修改源、测试、roadmap/modules/根README或其他R3资料，不执行Git写操作。

关联整合验证：179文件、5186案例，5129PASS/57SKIP/0FAIL，pytest退出0，432源码＋179测试输入零漂移，原件为`r3-trace2-candidate-20261002-v1/focused-v2.xml`。
仅作为related整合组证据，不是Trace2独有覆盖、全仓、原生或商业GO；初次2FAIL及2项control PASS保留，最终仅修正环境。P1闭环569仍为其独立子范围，不累加计数。

## 6. 四图评审状态

四个mmd及相邻同名PNG均位于包根；统一验证已完成Chrome渲染。四图实际视觉复核全部通过；最终封存同时记录mmd与PNG字节身份及逐图复核结果。

| 图 | 原件 | 状态 |
|---|---|---|
| 架构 | [architecture.mmd](architecture.mmd) | Chrome渲染及实际视觉复核通过 |
| 审批与单次进程 | [approval-process.mmd](approval-process.mmd) | Chrome渲染及实际视觉复核通过 |
| 成功失败时序 | [success-failure-sequence.mmd](success-failure-sequence.mmd) | Chrome渲染及实际视觉复核通过 |
| raw到投影数据流 | [raw-projection-dataflow.mmd](raw-projection-dataflow.mmd) | 最终TB版Chrome渲染及实际视觉复核通过 |

mmd与主设计块逐字一致检查不等于真实渲染或视觉验收，当前四图实际渲染与视觉复核均已通过；初次LR宽图视觉FAIL原件私有保留。

## 7. 闭合所需后继证据

- [x] 独立闭环按最终受审字节完成；T2-PEER-01解决，原P1/RED完整保留。后续字节变化须重新复核。
- [x] 统一验证使用已安装Chrome渲染4图并逐图实际看图，最终mmd/PNG SHA已封存；初次宽图FAIL原件保留。
- 固定候选输入、新原批准、新fixture、attempt1及原两个selectors进行新Windows原生诊断；版本不匹配或unknown仍不得PASS。
- 原Git128具体根因及最小修复须保留失败、效果不确定性与独立readback证据，不靠格式见证推断成功。
- Windows消费者、R3真实质量、fullCommit完整业务及商用R1～R6分别验收，不由本包代替。
