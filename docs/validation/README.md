---
doc_type: validation-evidence
status: current
version: 49
code_revision: c64ebb5f24b3f1bdcc82c63e07bbb80f511ed71e
owners:
  - core
modules:
  - documentation
  - evals
  - smoke
related_adrs:
  - docs/adr/0019-controlled-model-smoke.md
  - docs/adr/0047-coding-eval-campaign-evidence.md
  - docs/adr/0048-controlled-real-eval-campaign-execution.md
  - docs/adr/0087-deterministic-offline-eval-suite-composition.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/smoke
  - tests/evals
  - tests/benchmarks/test_soak_many_threads.py
  - tests/benchmarks/test_soak_long_session.py
  - tests/benchmarks/test_soak_attempt.py
  - tests/benchmarks/test_soak_context_proof.py
supersedes: []
---

# Harnessix Code验证证据索引

## 1. 文档定位

本索引登记仓库内已冻结的确定性离线Suite、真实Provider Smoke与Coding Eval证据。它只说明证据身份、执行边界、结果和替代关系，
不把历史结论升级为当前版本的生产承诺。当前测试方法和发布判定以[测试与Eval规范](../testing-and-evals.md)为准，
当前运行时行为以[模块详细设计](../README.md#3-当前事实源)为准。

## 2. 证据清单

| 日期 | 证据 | 代码Revision | 固定环境与预算 | 冻结结论 | 关系 |
|---|---|---|---|---|---|
| 2026-10-01 | [Git产品业务闭包与恢复设计评审](git-business-backup-design-2026-10-01-v1/README.md) | `96584026bdf34c49c519834331b84043a6c03895` | 既有源码研究，零真实请求；五幅实际渲染图 | 设计草案、完整接口/字段/流程/失败与恢复矩阵；原文档问题保留，同策略治理302通过 | 设计评审而非产品实现；历史备份范围及材料容量待冻结，不关闭Git写接线或商用门禁 |
| 2026-09-30 | [原认证Patch与只读Git基准](git-baseline-2026-09-30-v1/README.md) | `018a4afabf270bbd94deb4cbb728794615d85225` | macOS原生Git/文件/SQLite；零真实请求；独立Wheel | 完整6176通过/111跳过；源码外132通过；受控变异5失败；用户Index/HEAD保护及8MiB完整观察通过 | 只读前置候选；带保护源的Windows原始观察明确拒绝，Commit/Checkpoint、备份闭合及R4仍开放 |
| 2026-09-30 | [固定Profile与Trusted安全输入反馈](trusted-input-feedback-2026-09-30-v1/README.md) | `9723b58688890672ec17ffd8d37d78507ed81ece` | 原70元及旧预留不变；零真实请求；独立Wheel | 原源码5失败保留；焦点104、治理302、源码外70通过；完整离线6085通过/111跳过 | 仅参数反馈候选，不关闭真实质量或商用门禁 |
| 2026-09-30 | [固定镜像与显式Engine评测宿主前置](docker-eval-host-2026-09-30-v1/README.md) | 生产基线`89b3cd1`、测试源SHA绑定 | 原Python/Node RepoDigest；同Engine显式入口；零模型请求 | 原宿主状态恢复；真实集成5通过、关联离线151通过，旧断言FAIL保留；默认Desktop注册仍Created | 仅评测宿主前置GO；不关闭默认Desktop、真实20 Trial、消费者Windows11、Beta或商用门禁 |
| 2026-09-29 | [固定不同版本三平台停机升级、完整恢复与回退](different-version-upgrade-2026-09-29-v1/README.md) | `ec356aa1555d6ad712819f3e02e210493f7d3135` | 唯一规范Wheel；原归档0.1.0→内部1.0.0rc1→原旧包；源码外三平台；零模型请求 | 唯一构建及三消费者终态成功；原Key/六库、稳定恢复身份和回退读写通过；首轮cp1252和Apple Git失败保留 | 固定版本对专项GO；消费者Windows11、真实编码、独立Beta及R1～R6整体商用门禁仍开放 |
| 2026-09-29 | [R1既有安全控制追踪与双Python专项](v1-safety-coverage-2026-09-29-v1/README.md) | `ae590042f52de7e08eafb51a1fa080b935afc6df` | 18组风险、54个原函数选择器；本地macOS双Python、零模型请求 | 各146通过/2原生Windows跳过；逐组及参数结果保留 | 仅控制追踪与本地锚点GO；原生、实际后端、R1整体及商用门禁保持 |
| 2026-09-29 | [认证重启同候选三平台PASS](authenticated-restart-three-platform-candidate-2026-09-29-v2/README.md) | `0ba1b8cdb4750bd7e3002ba471e21f12375df425` | 原认证Profile、500 Thread、五周期、资源前置匹配；零模型请求 | 三份原报告PASS并独立复算；双Python Benchmark各243通过/1平台跳过 | 固定认证重启场景关闭；旧FAIL、首轮unverified及R1整体/商用门禁保留 |
| 2026-09-29 | [认证重启第一轮候选与Mac资源不匹配](authenticated-restart-three-platform-candidate-2026-09-29-v1/README.md) | `9b0d1230e702aa72b0ea946e1d49eb19a28d2784` | 原认证Profile、三平台500 Thread、零模型请求 | Linux/Windows PASS；Mac同镜像但c3-m7→c5-m14，unverified；原报告独立复算一致 | 三平台NO_GO；原Profile和全部旧FAIL保留，不以更强硬件接受PASS |
| 2026-09-29 | [默认认证产品三平台重启基线与Profile冻结](authenticated-restart-three-platform-2026-09-29-v1/README.md) | `7cbe358ff0f22ea2bc0813478bb1c13a2b1e7c46` | 三平台500 Thread、五周期、原余量、零模型请求 | 三平台基线及封印Profile有效；双Python Benchmark各241通过/1原生跳过 | 尚非候选PASS；旧容量FAIL、R1整体及商用门禁保持 |
| 2026-09-29 | [操作Schema整改后默认产品真实模型合同](product-provider-operation-schema-2026-09-29-v1/README.md) | 验收`c8033e08a260cfde793bf9809f6979a833fc92d5`、产品`6a686fd` | 原驱动SHA、北京精确快照、70元原周期、1024输出Token/单次尝试；一次两个场景 | 离线两场景与越界负对照通过；真实审批修改及取消通过，5请求、22626/634 Token、估算0.100648元；原认证Store/Usage独立复核通过 | 有限产品合同GO；原0/20、完整20 Trial、Beta、Windows11及商用门禁仍开放 |
| 2026-09-29 | [Workspace Patch操作Schema与旧批准边界](workspace-patch-operation-schema-2026-09-29-v1/README.md) | `6a686fdd00162babd0dbaa8b0785186dd15c3cbc` | 双Python受影响回归；原Schema和正式Adapter；模型请求0 | 336字段组合及17焦点通过；双Python各2890通过/70跳过；原校验与旧批准拒绝保持 | 离线专项GO；旧真实FAIL、0/20及商用门禁保持，新的线上认证另行验证 |
| 2026-09-29 | [显式预算修复与默认产品真实Provider诊断](public-budget-product-provider-2026-09-29-v1/README.md) | `184fb125f6159de4202a64a525f6b5cc99f0ab97` | 双Python；实际stdio；原70元周期和官方北京快照；7个已结算请求 | 原预算红例修复，离线审批/取消通过；真实首场景缺mode失败、第二场景停止；新增估算0.149308元 | 不关闭有限认证、20 Trial、Beta或商用门禁；原失败保留 |
| 2026-09-29 | [Windows复杂业务状态备份恢复与严格原生分类](windows-business-state-recovery-2026-09-29-v1/README.md) | `dc3692abb08ebe9e2e9bf4d971af9eee395cf590` | 原生Windows Runner；本地Python3.12/3.13；零模型请求 | 保留原生82通过/2失败；合法错Key及元数据2/3分类修复；四个原生步骤成功，全Job按实际状态登记 | 专项GO，R1/R4及商用仍未关闭；不替代真实编码、Win11或不同版本升级 |
| 2026-09-28 | [Artifact原正文持久来源认证](authenticated-artifact-2026-09-28-v1/README.md) | `33a2fd25bf6f529d1019cf584e02673734369299` | macOS本机、独立Key与真实SQLite/Runtime、零模型费用 | Migration 0030新行来源认证与同Key跨重启、旧行拒绝；完整回归与发行物结论以目录原件为准 | 0.9.4a及整体0.9继续开放，不能据此宣称三平台或发布通过 |
| 2026-09-27 | [Archive许可证据及Windows夹具整改](license-evidence-2026-09-27-v1/README.md) | `1f483ceb267fe2d15ca4d53f794184fd6aa76ecc` | macOS arm64、全部777件、203原字节Blob；Provider零请求 | 4055 passed/32 skipped及167交叠专项、独立缓存重采集、准确源码Wheel/sdist Secret通过；pywin32 12件许可门禁返回1，当前Windows终态待验证 | release_blocked；不追认旧失败，不标记0.9或商用发布完成 |
| 2026-09-27 | [有界Secret扫描整改](secret-scan-2026-09-27-v1/README.md) | `ad2f8e226b674e4787ad0002f0d038a2a81ccfef` | macOS arm64、Python 3.13.8；精确Git归档，真实模型0请求 | 87专项、195有重叠回归、3996 passed/32 skipped；干净源码Wheel/sdist固定六规则零命中；前序0601ede六CI成功不外推到扫描候选 | 当前扫描CI未纳入终态；保留前序失败证据，0.9.4与后续发布门禁仍开放 |
| 2026-09-27 | [安全治理阶段证据](security-governance-2026-09-27-v1/README.md) | 候选`21b5eb1d57055f32ba2178c165b3ad46060ee7c7`，前序完整回归与CI为`a5fd57e` | POSIX本地；固定ScriptedProvider，真实模型请求0；原字节Manifest和CI身份 | 前序3904 passed/32 skipped；候选16/214/107三个有重叠专项组通过；前序CI五成功、Windows两项导入失败，修复版CI未纳入终态 | 仅候选专项证据，不关闭0.9.4或发布门禁，不把旧版本完整回归外推到新版本 |
| 2026-09-25 | [长会话Context三平台冻结阈值第二独立Run](soak-long-session-three-platform-candidate-2026-09-24-v1/README.md) | `a2245f568ff3cab71c63e97869e66c93a8ebc529` | Linux/macOS/Windows各自预冻结Profile，1000 Turn、5预热、199次压缩摘要；确定性Provider | 三份STARTED v2/Run/Report原件、24份文件摘要及独立复验再生报告均PASS；首轮Windows候选在300分钟工作流期限取消已登记 | 固定长会话场景（1000 Turn）工程护栏通过；0.9.3d六场景全部完成三平台基线与第二独立PASS |
| 2026-09-24 | [长会话Context三平台正式规模基线](soak-long-session-three-platform-2026-09-24-v1/README.md) | `39dda2bb218baed30dcf9b1326f899a3133f497b` | Linux/macOS/Windows Python 3.12、1000 Turn、5预热、199次压缩摘要；确定性Provider | 三平台Run/Attempt原件及18份文件摘要、1000条Turn样本最近秩重算通过；[CI 36015746312](https://github.com/carrie1988/Harnessix/actions/runs/36015746312)六实例成功 | 三平台基线与封印Profile；70846f5旧运行降级为诊断，第二独立Run待复验 |
| 2026-09-24 | [Artifact增长三平台冻结阈值第二独立Run](soak-artifact-growth-three-platform-candidate-2026-09-24-v3/README.md) | `39dda2bb218baed30dcf9b1326f899a3133f497b` | Linux/macOS/Windows各自预冻结Profile，2预热+300正式混合大小件；确定性Provider | 三份STARTED v2/Run/Report原件、24份文件摘要及独立复验再生报告均PASS；[CI 36015746312](https://github.com/carrie1988/Harnessix/actions/runs/36015746312)六实例成功 | 固定Artifact增长场景（300件）工程护栏通过；前两轮FAIL保留，不关闭其余0.9.3d门禁 |
| 2026-09-24 | [Artifact增长三平台正式规模基线v3（300件负载）](soak-artifact-growth-three-platform-2026-09-24-v3/README.md) | `6f55e747bdecca1c8550467621028dfcd02079b2` | Linux/macOS/Windows Python 3.12、2预热+300正式混合大小件、2925次正式分页、历史Artifact批量验证；确定性Provider | 三平台Run/Attempt原件及18份文件摘要、分位数、v3 Proof独立复核通过；[CI 36009364391](https://github.com/carrie1988/Harnessix/actions/runs/36009364391)六实例成功 | 300件正式基线与封印Profile；前两轮FAIL与全部旧证据保持只读，第二独立Run待复验 |
| 2026-09-24 | [长会话Context两平台正式规模基线与Windows诊断](soak-long-session-two-platform-2026-09-24-v1/README.md) | `46ca9a2006b5f857ecb55924ba969cf2e331187e` | Linux/macOS 1000 Turn、5预热、199次压缩摘要；确定性Provider | 两平台Run/Attempt原件及14份文件摘要、1000条Turn样本最近秩重算通过；Windows在measuring阶段以`soak_turn_timeout`失败仅留诊断Attempt；[CI 35971925613](https://github.com/carrie1988/Harnessix/actions/runs/35971925613)六实例成功 | 仅Linux/macOS基线与封印Profile；Windows为Session快照路径规模限制，场景门禁未关闭 |
| 2026-09-24 | [Artifact增长三平台第二轮候选性能FAIL](soak-artifact-growth-three-platform-candidate-2026-09-24-v2/README.md) | `d4a3ee20a8e17fab466c690e86a9a48991430398` | 同三份60件冻结Profile、2预热+60正式混合大小件；确定性Provider | 24份原件与Reader、分位数复核一致；macOS/Windows PASS，Linux单件780ms停顿致发布P99越限FAIL；[CI 35966150370](https://github.com/carrie1988/Harnessix/actions/runs/35966150370)六实例成功 | 第二轮候选FAIL；负载修复为300件使P99为近限子样本第3高值，FAIL原件保留 |
| 2026-09-24 | [Artifact增长三平台正式规模基线v2（60件负载）](soak-artifact-growth-three-platform-2026-09-24-v2/README.md) | `5f1b3c1152c2eb7577b9265efa63f2ec1f186cdc` | Linux/macOS/Windows Python 3.12、2预热+60正式混合大小件、585次正式分页；确定性Provider | 三平台Run/Attempt原件及18份文件摘要、分位数、v3 Proof独立复核通过；[CI 35964147780](https://github.com/carrie1988/Harnessix/actions/runs/35964147780)六实例成功 | 负载修复后新基线与封印Profile；首轮FAIL与v1基线保持只读，第二独立Run待复验 |
| 2026-09-24 | [Action恢复三平台冻结阈值第二独立Run](soak-action-recovery-three-platform-candidate-2026-09-24-v1/README.md) | `65cbcc5caab2ba167763844e974c9cd7c036ed0d` | Linux/macOS/Windows各自预冻结Profile，2预热+20正式轮固定四故障矩阵；Provider零请求 | 三份STARTED v2/Run/Report原件、24份文件摘要及独立数值重算均通过；各平台Report为PASS；[CI 35961875693](https://github.com/carrie1988/Harnessix/actions/runs/35961875693)六实例成功 | 固定Action恢复场景工程护栏通过；不关闭其余0.9.3d或1.0发布门禁 |
| 2026-09-24 | [Artifact增长三平台首轮候选性能FAIL](soak-artifact-growth-three-platform-candidate-2026-09-24-v1/README.md) | `65cbcc5caab2ba167763844e974c9cd7c036ed0d` | 同三份冻结Profile、2预热+20正式混合大小件；确定性Provider | 24份原件与Reader、分位数复核一致；Windows PASS，Linux/macOS发布与读取尾部越限FAIL；[CI 35961875693](https://github.com/carrie1988/Harnessix/actions/runs/35961875693)六实例成功 | 该Revision固定场景候选FAIL；诊断为共享Runner I/O漂移，负载修复为60件后须新基线与新候选，FAIL原件保留 |
| 2026-09-24 | [Action恢复三平台正式规模基线](soak-action-recovery-three-platform-2026-09-24-v1/README.md) | `b3e0d6f731d29b2749aab2827a9ebb302c2e87d6` | Linux/macOS/Windows Python 3.12、2预热+20正式轮固定四故障矩阵、44次预期UNKNOWN；Provider零请求 | 三平台Run/Attempt原件及18份文件摘要、分位数、v7 Proof独立复核通过；[CI 35960330442](https://github.com/carrie1988/Harnessix/actions/runs/35960330442)六实例成功 | 三平台各一次正式基线与封印Profile；第二独立Run与报告待复验，基线本身不判PASS |
| 2026-09-24 | [Artifact增长三平台正式规模基线](soak-artifact-growth-three-platform-2026-09-24-v1/README.md) | `b3e0d6f731d29b2749aab2827a9ebb302c2e87d6` | Linux/macOS/Windows Python 3.12、2预热+20正式混合大小件、195次正式分页；确定性Provider | 三平台Run/Attempt原件及18份文件摘要、分位数、v3 Proof独立复核通过；[CI 35960330442](https://github.com/carrie1988/Harnessix/actions/runs/35960330442)六实例成功 | 三平台各一次正式基线与封印Profile；第二独立Run与报告待复验，基线本身不判PASS |
| 2026-09-23 | [多Thread读路径收敛后三平台候选原件](soak-many-threads-three-platform-candidate-2026-09-23-v3/README.md) | `aa3372c0eb0c3b4ab674b19d26754a80dd035b46` | 原三份封印Profile、500 Thread/50每页/3正式重启；Provider零请求 | 三平台Run/Attempt/Report 24份原件、三个ZIP、逐轮Proof、最近秩与独立报告复验均为PASS；[同Revision CI 35883976180](https://github.com/carrie1988/Harnessix/actions/runs/35883976180)六Job成功 | 固定多Thread场景工程护栏通过；旧macOS FAIL原件保留，不外推到产品发布 |
| 2026-09-23 | [多Thread三平台修复后候选性能FAIL](soak-many-threads-three-platform-candidate-2026-09-23-v2/README.md) | `807e688988245fcbb269f0c04013ed9a9ca6ea9d` | 同三份冻结Profile、500 Thread/50每页/3正式重启；无模型请求 | 24份原件与三个ZIP、Reader、逐页Proof、分位数和独立报告复验均通过；Linux/Windows PASS，macOS启动P95/P99和分页P99越限FAIL；[同Revision CI 35880201885](https://github.com/carrie1988/Harnessix/actions/runs/35880201885)六Job成功 | 该Revision固定场景候选FAIL；不得调高原Profile或丢弃macOS FAIL，新Revision验收见首行 |
| 2026-09-23 | [多Thread三平台首轮候选诊断](soak-many-threads-three-platform-candidate-2026-09-23-v1/README.md) | `cadc4e5190dd94e9dbc7760b19b1659090f73cbb` | 同冻结Profile三平台第二Run | 三份报告PASS，24份原件与三个ZIP及独立复验一致；同Revision CI跨平台夹具错误失败 | 不作为正式场景关闭证据；修复后重测见上一行 |
| 2026-09-23 | [多Thread三平台v6逐轮证明正式基线及封印Profile](soak-many-threads-three-platform-2026-09-23-v2/README.md) | `6e64a5cba2108ac77de90a5726b45773b4482c75` | Linux/macOS/Windows各500 Thread、50条每页、一次预热、三次正式Runtime/App Service重启；无模型请求 | 三平台Run/Attempt与v6 Proof、18份原件SHA、三个ZIP及最近秩统计独立复核通过；[同Revision CI 35876009038](https://github.com/carrie1988/Harnessix/actions/runs/35876009038)六Job成功；三份Profile从基线封印并独立重读 | 仅单次正式基线及预冻结阈值，另有第二轮候选macOS FAIL；不判场景PASS |
| 2026-09-23 | [多Thread三平台一次正式负载基线](soak-many-threads-three-platform-2026-09-23-v1/README.md) | `12bfc1108efc461742712c98fe782ce4b1849f05` | Linux/macOS/Windows各500 Thread、1次预热、3次正式Runtime/App Service重启，50条每页；无模型请求 | 三平台Runner成功；Run/Attempt与15份原件SHA、最近秩统计重算通过；[CI 35870214455](https://github.com/carrie1988/Harnessix/actions/runs/35870214455)六Job成功 | v1无逐轮集合Proof，超时排空无独立全局期限；不能冻结Profile或判场景PASS |
| 2026-09-23 | [完整产品重启三平台冻结阈值第二独立Run](soak-restart-three-platform-candidate-2026-09-23-v1/README.md) | `e81ebada78f67f4c447e4ae0089ec143ccd6cf34` | Linux/macOS/Windows各自预冻结Profile，500 Thread、1次预热、1次受控硬退出、3次正式新进程；Provider零请求 | 三份STARTED v2/Run/Report原件、24份文件摘要及独立数值重算均通过；各平台Report为PASS；[CI 35867509424](https://github.com/carrie1988/Harnessix/actions/runs/35867509424)首次文档Job超时、只重跑失败Job后六Job成功 | 仅固定重启场景工程护栏；不关闭其余0.9.3d或1.0发布门禁 |
| 2026-09-23 | [完整产品重启三平台正式规模基线](soak-restart-three-platform-2026-09-23-v1/README.md) | `172b1ee96a89e981a6332b16f60b86e2db654df1` | Linux/macOS/Windows Python 3.12、500 Thread、1预热+1硬退出+3正式新进程；Provider零请求 | 三平台Run/Attempt原件及18份文件摘要、分位数、V5 Proof独立复核通过；[CI 35864457452](https://github.com/carrie1988/Harnessix/actions/runs/35864457452)失败文档Job重跑后六Job成功 | 三平台各一次正式基线与封印Profile；第二独立Run与报告见上一条，基线本身不判PASS |
| 2026-09-23 | [Windows Product UI退出期限诊断](product-quit-windows-2026-09-23-v1/README.md) | `823ceac0c7ec250bb36cd0009946949ad8f094d2` | [CI 35845010214](https://github.com/carrie1988/Harnessix/actions/runs/35845010214) Windows原生Job，首次与第二次尝试 | 首次511通过/1失败/45跳过，重跑512通过/45跳过；外层10秒小于子进程默认10+5秒关闭预算 | 仅诊断，预算倒挂修复由[CI 35847851813](https://github.com/carrie1988/Harnessix/actions/runs/35847851813)三次六Job成功验收；旧首次失败排他根因仍未确认，不宣称整体稳定 |
| 2026-09-23 | [SDK容量三平台冻结阈值第二独立Run](soak-sdk-three-platform-candidate-2026-09-23-v1/README.md) | `cf7e6b4dba5357354abcd3822bcb2c1e2215bd7c` | Linux/macOS/Windows各自预冻结Profile，固定64容量、1预热+3正式轮、20次往返；Provider零请求 | 三份STARTED v2/Run/Report原件、24份文件摘要和独立数值重算均通过；各平台Report为PASS；[CI 35843734178](https://github.com/carrie1988/Harnessix/actions/runs/35843734178)六实例成功 | 仅SDK容量固定场景工程护栏；不关闭其余0.9.3d或1.0发布门禁 |
| 2026-09-23 | [SDK容量三平台正式规模基线](soak-sdk-three-platform-2026-09-23-v1/README.md) | `70c5161986082b63acd31ab1acf8328c9fad9efd` | Linux/macOS/Windows Python 3.12、容量64、各1预热+3正式轮、各20次往返；Provider零请求 | 三平台Run/Attempt原件及18份文件摘要、分位数、Proof独立复核通过；[CI 35838258049](https://github.com/carrie1988/Harnessix/actions/runs/35838258049)六实例成功 | 三平台各一次正式基线与签封Profile；第二独立Run与报告单独见上一条，不得由基线本身判PASS |
| 2026-09-23 | [macOS SDK容量单平台规模基线](soak-macos-sdk-2026-09-23-v1/README.md) | `c4c364c059a7b6ea61410fe03ba41ef140fcdd41` | macOS Python 3.13.8、`c16-m48`、容量64、1预热+3正式轮、20次正常往返；固定Provider零请求 | Run/Attempt与独立摘要、样本、Proof复核通过；对应Revision六实例CI成功；往返P95 1407042 ns、父子峰值RSS较大者68845568字节 | 单平台单次基线；未冻结阈值、第二独立Run或Linux/Windows正式负载，不得判发布PASS |
| 2026-09-23 | [macOS Artifact单平台规模基线](soak-macos-artifact-2026-09-23-v3/README.md) | `5d48b9735c11022743eb56df5da23125702b0140` | macOS Python 3.13.8、`c16-m48`、2预热+20正式件、20发布/195正式读取样本；无模型网络请求 | Run/Attempt与独立数值重算通过；对应Revision六实例CI成功；发布P95 13654625 ns、读取P95 6604000 ns | 单平台单次基线，可供工程Profile评审；未冻结阈值、未做独立复验或其他平台正式运行，不得判发布PASS |
| 2026-09-23 | [macOS Artifact第二次规模诊断](soak-macos-artifact-2026-09-23-v2/README.md) | `c63f970f3386632b9720ae33d1a7f056e70c0510` | macOS Python 3.13.8、`c16-m48`、2预热+20正式件、20发布/195正式读取样本；无模型网络请求 | Run/Attempt重读和独立分位数/覆盖重算通过；对应Windows CI的Turn超时用例出现异步SQLite句柄清理失败 | 历史诊断证据；不得冻结Profile或判发布PASS |
| 2026-09-23 | [macOS Artifact首次规模诊断](soak-macos-artifact-2026-09-23-v1/README.md) | `784cd54ec2383c6a3679d64db401ad4d3bf9b86a` | macOS Python 3.13.8、`c16-m48`、2预热+20正式件、20发布/195正式读取样本；无模型网络请求 | Run/Attempt重读和独立数值重算通过；对应Windows CI的只读SQLite连接未关闭 | 历史诊断证据；不得冻结Profile或判发布PASS |
| 2026-09-23 | [macOS 500 Thread正式负载诊断](soak-macos-2026-09-23-v4/README.md) | `d947a57aec68a5f9770a18d1996f58be0237e60d` | macOS Python 3.13.8、`c16-m48`、500 Thread、3次正式重启、30个列表页样本；无模型请求 | Run/Attempt及独立标准库重算通过；启动P95 435170625 ns、列表页P95 431874209 ns；Windows Product UI首次尝试两项超时、第二次重跑成功 | 历史诊断证据；首次失败原因未明，不得冻结Profile或判断发布PASS |
| 2026-09-23 | [macOS千Turn Context/Compaction v2规模基线](soak-macos-2026-09-23-v3/README.md) | `6c9a1577f467c99f0eb1b99d7c8270bc811ca583` | macOS、单Thread、5预热+1000正式Turn、无网络模型；1000时延和1个RSS样本、13255条低敏事件标记 | Run/Attempt复制件双重重读，正式Context检查1000、摘要及窗口199；P95 3153631375 ns、RSS峰值451936256字节；六实例CI通过 | 单平台单次基线；不得直接冻结Profile或判发布PASS |
| 2026-09-23 | [macOS单Thread连续1000 Turn诊断事实](soak-macos-2026-09-23-v2/README.md) | `ed48e4e35133d60b172c268becd9e009eacb9442` | macOS Python 3.13.8、`c16-m48`、5预热+1000正式Turn、1000时延样本与1个RSS样本；无网络模型请求 | Run及Attempt复制件独立重算，P95 1173980291 ns、RSS峰值344817664字节；Revision六实例CI通过，但缺Context/Compaction专门断言 | 真实规模诊断事实，不用于冻结Profile，不关闭0.9.3d发布门禁 |
| 2026-09-23 | [macOS 500 Thread单次Soak诊断事实](soak-macos-2026-09-23-v1/README.md) | `d64054638a03bca008f5b392fd0edf23aa4cd63b` | macOS Python 3.13.8、`c16-m48`、500 Thread、3次正式重启、30个列表页样本；无模型请求 | Manifest和原始数值已独立重算；启动P95 440247292 ns、列表页P95 427911708 ns；对应Revision跨平台CI未通过 | 历史诊断证据，不用于冻结Profile，不关闭Soak或商用发布门禁 |
| 2026-09-20 | [工程Task Pack v2真实Provider完整Suite](provider-engineering-2026-09-20-v1/README.md) | `fb4a0ea8f7ffcd14113212fb77b2028143af9914` | 北京精确模型、3仓10 Case、每Case 2 Trial、无自动重试、CNY 40 Trial边界停止线 | 20/20 Trial终结；任务成功0/20、测试通过0/20；81请求、318,478/12,148输入/输出Token、CNY 1.46828完整已知成本 | 0.9.2e当前冻结真实质量基线；证明失败可审计，不证明模型可用成功率 |
| 2026-09-20 | [工程Task Pack v2完整离线Suite](offline-engineering-2026-09-20-v2/README.md) | `505bc537f74bd59e891c605ff4114856991f1783` | 3仓10 Case、每Case 2 Trial、固定Digest无网Container、Recorded Provider零费用 | 20/20 Trial通过；120请求、60自动审批；首Case证据与Suite报告两个崩溃窗口均恢复 | 0.9.2d3当前冻结离线证据；不证明真实模型能力 |
| 2026-09-03 | [百炼北京受控Smoke](bailian-2026-09-03.md) | `9f24961840fa704e7c7a344c648164d8afe793b7` | 北京兼容端点；每请求最多128输出Token；零重试；总计7次请求 | 文本、内存工具、审批重开三个固定场景通过；费用和其他Provider未验证 | Smoke独立证据 |
| 2026-09-06 | [Coding Eval v1](bailian-2026-09-06-coding-eval/README.md) | `bbfd446707acbd9f945657ad96a6556d24af5df5` | 固定模型；3个Run；16步骤、20000累计Token、600秒；费用停止线¥10 | 0/3，均因任务Token预算结束；不能形成编码成功率 | 被v2诊断推进，但原证据保留 |
| 2026-09-06 | [Coding Eval v2](bailian-2026-09-06-coding-eval-v2/README.md) | `397542942be8474d99feb190a901e8b336a19bdd` | 固定模型；3个Run；100000累计Token；费用停止线¥10 | 0/3，暴露分页参数错误反馈不可自纠正 | 被v2纠正后Campaign推进 |
| 2026-09-06 | [Coding Eval v2纠正后](bailian-2026-09-06-coding-eval-v2-corrected/README.md) | `7e58c15a4be9780de5837c9706f986a8613b1420` | 同等固定模型、Run数和预算；加入专用分页纠错 | 严格0/3；其中2/3完成代码闭环，但模型不可见最终回答Schema | 被v3消除测量偏差 |
| 2026-09-06 | [Coding Eval v3](bailian-2026-09-06-coding-eval-v3/README.md) | `9d0be66e197a82506cf0d0dcbf59832d8865f1e2` | 同等固定模型、Run数和预算；Prompt公开最终回答Schema | 固定历史缺陷3/3严格通过；不得外推到任意仓库或任务 | 该任务序列的最新冻结证据 |

## 3. 证据谱系

```mermaid
flowchart LR
    Smoke[2026-09-03 Smoke] --> SmokeConclusion[三个固定场景通过]
    V1[Eval v1] --> Budget[预算不适配]
    Budget --> V2[Eval v2]
    V2 --> Feedback[错误反馈不可自纠正]
    Feedback --> V2C[Eval v2纠正后]
    V2C --> PromptGap[模型不可见评分Schema]
    PromptGap --> V3[Eval v3]
    V3 --> Baseline[固定任务3/3严格通过]
    Offline[2026-09-20工程离线Suite] --> OfflineResult[20/20执行与恢复通过]
    OfflineResult --> Provider[2026-09-20真实Provider Suite]
    Provider --> ProviderResult[0/20严格质量基线]
```

图中的箭头表示问题诊断和任务版本演进，不表示后一个目录覆盖前一个目录。所有失败、预算终止和测量偏差均保留，
以便复查架构决策如何形成。

## 4. 固定字段与脱敏边界

每份证据必须固定或显式声明：

1. 代码Revision、执行日期、Provider端点/地域、协议和精确模型；
2. Task、Campaign、Run、参数、重试、Token、时间、请求次数和费用边界；
3. 完成、失败、取消、预算结束和未知成本的分类；
4. 证据文件摘要、聚合方式、适用结论和不能外推的范围；
5. 凭据、Header、供应商正文、私有Session、工作区和工具输出的保存策略。

仓库只保存允许公开的白名单投影。API Key、Authorization Header、供应商响应正文、私有Session和个人环境路径
不得进入Git历史。历史证据发现脱敏缺陷时，应先阻断发布并清理敏感数据；不得以“保持历史原文”为由保留Secret。

## 5. 使用与维护规则

1. 日期化证据生成后保持`historical`，不得覆盖原JSON、Run身份、结果或失败分类；
2. 后续验证创建新目录或新文档，并在本索引记录谱系和适用Revision；
3. 新证据可以取代某项结论，但不能删除产生该结论的旧失败证据；
4. 费用为基于版本化价格和完整Usage的估算时，必须明确不等同于供应商账单；
5. 单个Provider、地域、模型和任务通过，只证明该固定组合，不构成产品级泛化结论；
6. 当前结论必须同时核对对应模块设计、路线图和最新CI，不得直接引用历史数字作为发布状态。

- [用户输入持久前与命令回执保护](input-persistence-2026-09-28-v1/README.md)：原Runtime入口、Claim前、回执和批准审计边界；历史授权及Protocol原始帧出口独立开放。

- [原协议帧与握手提交证据](protocol-frame-publication-2026-09-28-v1/README.md)。

- [导出查询边界验证证据](query-publication-2026-09-28-v1/README.md)。

- [认证历史与Event Seal核心](event-seal-core-2026-09-28-v1/README.md)：核心验证与实际默认Root开放观察，非发布验收。

- [认证SQLite Session](authenticated-sqlite-session-2026-09-28-v1/README.md)：真实同事务显式库合同，默认产品与Key Backend验收独立开放。

- [默认产品持久Session密钥](managed-session-key-2026-09-28-v1/README.md)：强制认证Root、实际macOS保护与真实CLI/SDK重开；Windows和发行验收独立开放。

## 固定Profile默认参数兼容

[验证交付](profile-approval-default-2026-09-30-v1/README.md)保留真实Suite两请求后的合法默认参数误拒绝、原预算结算与未完成状态，记录正式解码器修正及286项离线/6项真实容器回归；真实质量、既有CI风险和商用门禁继续开放。

## 工程Suite中断与费用待核对

[中断验证](provider-suite-interruption-2026-09-30-v1/README.md)对应固定`0813c58`的新20 Trial预注册，实际完成4 Case/8 Trial，无严格通过；后续模型输出失败触发原预算未知保护。公开有限事实、原文件摘要与Review Packet，不把部分结果作为完整Suite成绩，不自动退款或继续模型请求。

## 唯一子进程回收与077评测恢复

[专项验证](single-child-reaper-2026-09-30-v1/README.md)包含原生Watcher竞争复现、失败后备仅发组信号、当轮Windows测试就绪标记方案，以及077下新Workspace精确模式和录制Oracle的Git权限语义。原FAIL与最终关联结果分别保存；当前Windows就绪合同为PID正文关闭后独立空标记，见后继[详细设计](../changes/m09-r1-single-child-reaper.md)。不是完整消费者平台、模型质量或商用发布验收。

## 严格工具参数反馈与原生就绪

[完整专项交付](tool-argument-feedback-2026-09-30-v1/README.md)包含9份原Session只读回放白名单投影、
静态字段反馈、双Provider离线SDK修正/真实Artifact读取/重开回放、原固定源码RED与关联回归。
上一候选的原生CI与本轮后继源码分别绑定；功能测试与许可证Job失败区分，不以部分通过宣称商用完成。

## 固定Profile观测分类与缺证停止

[统一验证交付](profile-observation-stop-2026-09-30-v1/README.md)从原Session只读副本复现
参数拒绝误计为检查的故障，记录107项新增专项、原源码50项失败负对照、
完整6283通过/111跳过、实际Wheel124项安装回归及4项固定容器/录制链验证。
指定证据缺失持久停止Campaign与Suite，重开不重放；原账本、未知效果和质量门槛不变。
新完整Suite只有前置准备，未取得新的真实20 Trial成绩，不关闭R3或其他商用门禁。

## 同一剩余额度的单次Suite切换

[统一验证报告](reverification-suite-binding-2026-09-30-v1/README.md)记录固定脚本146项专项、
全Eval566项及源码外产品依赖241项回归，含未经删改旧Reader拒绝和三个实际管理进程退出窗口。
原umask077失败及同选择器022通过均保留；原实际账本未改、新增付费请求为0。
该实现不生成第二轮40元额度，不代替实际授权登记或新的完整真实质量成绩。


## Windows Git原始观察认证与安全输出

[统一交付包](windows-raw-git-observation-2026-09-30-v1/README.md)记录v2回执、原终态MAC/Lease重验、
正式Git基准和双版本完整备份。公开代码绑定、原失败谱系、分层测试、源码外安装、
结构收敛和原生Windows测试范围；不把macOS跳过、新候选CI或rc版本号作为商用通过。

## 产品Git来源根目录生命周期

[专项交付](git-source-root-lifecycle-2026-09-30-v1/README.md)保留固定候选Windows原生126通过、
1失败和2跳过，区分活动根的系统句柄保护与退出后新根的来源身份拒绝。
双Python和实际安装Wheel各69项通过；后继Windows结果单独绑定，不关闭真实质量或商用门禁。

[后继实际Windows原生证据](windows-raw-git-native-2026-09-30-v1/README.md)确认根生命周期首组通过，
新增19项raw/Git原生通过；第三组整体仍失败并保留原日志，不能称全Job或R4通过。

## Git交付账本只读访问

[统一专项交付](git-store-readonly-2026-09-30-v1/README.md)记录原v1格式、真实Git领域记录、
真实WAL读取、五写入口首步拒绝及结构损坏关闭。新接口不创建产品Git目录、不扩展现有备份白名单，
全事件前缀认证、对象材料和正式Commit/Checkpoint接线仍未完成；不替代R3/R4商用门禁。


## Git受控命令IO与POSIX原始回执

[统一交付报告](git-supervised-io-2026-10-01-v1/README.md)记录正式内部命令端口、
POSIX pipe V2原始双流认证、原同步门面回归与源码外制品输入。保留原V1阻断及
测试夹具失败，区分原始观察、脱敏持久正文、批准和命令层完成事实。
不发布完整Git产品能力或R3成绩，不降低8MiB对象材料、全业务备份和商用门禁。

## 固定Git对象完整材料读取

[统一交付包](git-object-material-2026-10-01-v1/README.md)记录唯一batch/OID用途、原8MiB真实对象读取、
原批准及Owner认证、标准1MiB不变、等字节保护命中原失败及一次性快照修复。
实际Wheel、源码外双Python与完整验证输入逐字节绑定；原生Windows取消夹具保留强PID/MAC/EOF合同。
该读取增量不关闭8MiB输入、对象CAS及认证账本、全业务备份、R3/R4或商用1.0。

## Git响应注入夹具输入完成与原生失败

[专项交付](git-material-fixture-eof-2026-10-01-v1/README.md)记录固定Windows作业原62项IO通过、
材料208通过/1失败以及强PID实际通过范围，保留完整原日志摘要和未执行的输出断言。
后继仅使固定响应程序先消费唯一OID至EOF，保留209原案例并增加错误OID负对照；
本地与新原生结果独立登记，不发布新R3成绩或完整Windows/商用通过结论。
