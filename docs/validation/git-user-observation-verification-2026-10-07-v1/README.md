---
doc_type: validation-evidence
status: current
version: 1
code_revision: 82c95e677d1919c60bbb3be32a9a4ef23f35b2e4
owners: [core]
modules: [product_config, session, delivery, execution, documentation]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_user_observation_verification.py
  - tests/product_config/test_git_observation_verification_recipe.py
  - tests/product_config/test_git_observation_intent_flags_verification.py
  - tests/product_config/test_git_checkpoint_preparation.py
  - tests/product_config/test_git_baseline.py
  - tests/session/test_authenticated_history.py
supersedes: []
---

# 阶段无关用户 Git 观察：只读复核与原准备器回归

## 1. 结论与需求背景

**`READONLY_DEPENDENCY_VERIFIED_NOT_RELEASE`：同一最终安装包的345项唯一核心测试通过；仅验收阶段无关只读依赖。**

本切片为 R4/B3 所需的只读依赖，不是完整 B3 决策接线或 Git 写交付。
既有 prepare 只接受原 pending Call；审批决定后的宿主不能借该阶段约束重建旧材料。
新增内部 `verify_product_git_user_observation` 重读实际完整认证历史并复核原 U，
与原准备器共用末轮事实配方，不重新 collect Source、不写 CAS、不追加批准或效果。
[总体与详细设计](../../changes/m09-r4-git-user-observation.md#10-阶段无关的原观察只读复核)
给出接口、字段、流程、时序、伪代码、失败语义及源码追踪；
[approved-link](../../changes/m09-r4-git-approved-link.md)仍为后继 draft。

## 2. 固定源码、包及运行环境

父提交 `82c95e677d1919c60bbb3be32a9a4ef23f35b2e4` 仅为研究和克隆基线，
不冒充新增实现提交；实际字节由[输入全集](source-inputs.json)及[测试增量](extra-test-inputs.json)固定。
1446件源、测试、脚本、治理及锁文件在扩展前后无漂移，新增1件仅测试的debug分支负控，产品源码未再变。
Wheel SHA256为 `02750deb202d3a98c22f5339ff32e69454baa85e4e28a15d05a6b215d9d77eba`。
548件包成员、其中507件Python源码，与固定候选和独立安装逐字节一致，见[安装证明](installed-content-proof.json)。

环境为本机 macOS/arm64、CPython3.12.7、`1.0.0rc1`、Git2.53.0。
依赖从原 `uv.lock` 的 all-extras及dev组导出，使用离线 `--require-hashes` 安装；
这是验证环境，不是仅运行依赖的消费者安装或Linux/Windows原生证明。
安装测试使用独立venv和 `python -I`，无 `PYTHONPATH`，导入来自site-packages；
测试代码从固定候选读取，隔离子进程同样可导入实际安装包，不借父进程源码fallback。

## 3. 本次实际修复

1. 深重建完整预期观察，在原Session重新认证完整Thread/events，首末与原资源和实现绑定比对。
2. 公开baseline digest与观察fingerprint可重算；必须向原Reader核对绑定，逐成员复用原树/OID/mode、Index stage、`-v`、debug flags及before正文完整SHA/长度验证。
3. 原生Workspace根身份不等于Git实际工作树根；复用原 `--show-toplevel` 查询及helper配置拒绝，阻断 `core.worktree` 重定向和父仓库发现。
4. 回调正常返回后仍检查取消、原预算和Reader/Scope/读取回调替换；保留实际Session回滚/关闭结算失败优先级。
5. 原准备器只抽取共享末轮配方，保留pending Call、原注入位置、异常归属与调用顺序。

## 4. 最终验证与覆盖

| 验证组 | 通过 / 失败 / 错误 / 跳过 | 秒数 |
|---|---:|---:|
| 新入口与共享配方 | 90 / 0 / 0 / 0 | 279.044 |
| 原准备器、Source、观察及Session | 191 / 0 / 0 / 0 | 405.841 |
| 原基准算法 | 63 / 0 / 0 / 0 | 23.393 |
| 独立debug flags入口负控 | 1 / 0 / 0 / 0 | 1.066 |

四组逐nodeid无重叠，共345项唯一核心测试；不是全仓通过数。治理回归结果见[结构化结论](result.json)，不与核心集合混计。mypy507源文件、原可读性策略检查通过；只更新当前源码快照，不放宽策略。

文档、可读性及生成合同的29项治理回归全部通过。
现行文档门禁检查587份文档、13397条链接及变化图的实际渲染，零finding；
1108为全仓Mermaid代码块识别数，不表示全部1108幅图都重新渲染。
固定Secret规则完整扫描5109个候选源码/资料/发行物输入，零命中；该结果不覆盖私有原始测试日志。

各组采用同一最终Wheel与冻结产品源码；测试集合逐nodeid区分，不将早期v1、诊断或重复运行相加。
正式SDK阳性只替网络Provider，认证Session、Patch2、Source2、Router/CAS和Git读取端口均实际运行。
纯配方测试使用模拟端口，只证明顺序、错误及pin关闭，不作为MAC认证正例或模型质量结果。
状态断言覆盖Session及四库逻辑内容、路由/请求数、CAS成员身份/正文、原物理Index及工作文件；
不以SQLite WAL布局或访问时间不变冒充完整读写证明。

| 必需边界 | 验证方法 |
|---|---|
| SHA1/SHA256与真实SDK阳性 | 原MAC成功Patch2及真实Git收集后只读复核，包含深父闭包、连续Patch和重开 |
| 公开摘要不能认证 | reader_binding/member OID替换后修复全部公开摘要，必须由原生事实拒绝 |
| 实际Git根 | config/status摘要均修复后的core.worktree重定向；父仓库发现并记录实际根查询 |
| before及Index语义 | 真实loose blob同OID正文损坏、assume-unchanged、skip-worktree、intent-to-add |
| debug独立分支 | 空HEAD blob保证stage与-v均匹配，证明到达debug查询且非零flags拒绝，而非先在stage失败 |
| 操作期权威漂移 | 首次Git返回及最终正常回调时替换Reader绑定/固定参数/执行文件，须拒绝 |
| 取消、超时及回收 | 入口停止、在途实际读的取消/父Task/原绝对期限、正常回调隐藏停止，保持原异常与预算 |
| 结算失败优先级 | 实际sqlite authorizer拒绝ROLLBACK；实际close后注入OSError，分别与取消/Owner错误组合 |
| 只读及兼容 | 禁止新collector/put_blob/route/批准，原prepare顺序、pending与旧基准算法回归 |

close注入项只证明关闭后的结算异常优先级，不夸称真实操作系统close故障。
旧非空HEAD intent-to-add负例可能先在stage拒绝；新增空HEAD负例独立证明debug分支。

## 5. 原始失败、修复与环境分层

完整失败XML/日志分别封存在专用受控验证目录；公开[运行摘要](run-history.json)保存统计与原件SHA。
不覆盖旧FAIL，不通过删除测试、降低门槛或扩大期限获得绿测。

- API缺失、配方缺失及早期深快照同对象断言失败保留；后者改为值相等，并独立检查不复用调用方别名。
- reader_binding/member伪造四项真实RED为 `DID NOT RAISE`；旧Git导致的初始化错误不作为安全RED。
- core.worktree重定向真实RED为 `DID NOT RAISE`；补原实际根查询后拒绝，父仓库负控另行实测。
- 初次191项回归180通过/11失败：只装依赖没有安装Harnessix，`python -I`受管子进程无法导入包，继而触发 `process_not_owned`。环境修复为独立Wheel安装，不改产品契约。
- 初次mypy缺7个OpenTelemetry导入；按原锁补all-extras验证依赖，未修改生产依赖或锁文件。
- before损坏夹具首次遇到只读Git对象的PermissionError；测试改为同目录replacement/os.replace并保留原mode，不放宽产品权限。
- 原基准追加回归首次误用Apple Git2.24.3，63项初始化失败；已完成原运行，停止脚本未找到活跃PID，不声称中途取消。后继组显式绑定原Git2.53.0。
- 初次治理29项27通过/2失败：低敏索引尚未生成导致链接缺失，且可读性最终快照未同步新增方法；完成资料生成并更新当前快照，原策略和阈值不变。
- Mermaid首次因CLI预期Chrome缓存缺失而失败，保留原件；使用既有Google Chrome的独立headless实例渲染，不下载浏览器或改产品。

早期安装v1的191/74项通过只作为历史底座，不能代替根检查修订后的最终v2。

## 6. 图像、审查与源码阅读

[流程图](artifacts/verification-flow.png)、[时序图](artifacts/verification-sequence.png)来自正式详设第10节实际渲染。
变化的两份详设共11幅图渲染完成；新增两幅图逐图视觉核验节点、中文、调用顺序及边界说明。
[Review Packet](review-packet.md)将原缺陷、后继静态评审、实际测试、来源及开放门禁分开。
关键入口位于[git_user_observation.py](../../../src/harnessix/product_config/git_user_observation.py)，
共享接线位于[git_checkpoint_preparation.py](../../../src/harnessix/product_config/git_checkpoint_preparation.py)，
原成员算法位于[git_baseline.py](../../../src/harnessix/product_config/git_baseline.py)。

## 7. 未完成范围与发布限制

仅本机只读依赖通过不关闭完整B3、B4/B7连续终端见证、approved Writer、NativeBridge、A/T2/D、Commit或Backup2。
同步CAS/完整历史解码的事件循环响应性P1仍开放，外层async timeout不能即时抢占同步读取。
多次只读观察不是跨SQLite/文件系统原子锁，也不是持续执行权限；末轮之后的外部变化窗口仍存在。
正式R3质量、两笔未决费用、三平台消费者、独立Beta及同候选R1～R6保持开放。

[BETA-001](../../operations/pilot-tasks/001-login-password-protection.md)为初始源码独立副本任务登记，
`QUEUED/NOT_EXECUTED/NOT_EVALUATED`，真实Harnessix完成数0。
直接整改参考副本不计作Agent自动完成；原项目禁止任何写入，未新发模型请求、读取真实凭据或生产部署。
本次无Schema/DDL、Key、费用规则、工具广告、授权或网络配置变化。

## 8. 制品与复核

[结构化结论](result.json)、[输入全集](source-inputs.json)、[安装证明](installed-content-proof.json)、
[运行历史](run-history.json)、[制品清单](manifest.json)及[摘要文件](SHA256SUMS)构成公开低敏索引。
原完整日志、XML、实际Wheel/安装环境、依赖输入、所有失败及图像在专用验证目录保留。
公开安装证明去除本机绝对路径，仅保留导入层级与成员事实，不改写私有原件。
本报告不代表商用发布、用户签收或Linux/Windows原生验收。

### 8.1 同范围重放方法

使用固定候选或包含相同文件摘要的后继提交，不在原AIPracticalPlatform目录执行本节命令。
先通过可信来源核对Wheel摘要，再以原锁all-extras/dev的[安装输入](requirements-all.txt)建立独立venv。
离线缓存缺失时停止，不能删除哈希、跳过依赖或借源码导入取得绿测。
以下环境变量分别指向Harnessix候选、独立安装Python及证据输出；Git必须为已核验的2.53.0。

```bash
export SOURCE="/absolute/frozen-harnessix-candidate"
export PY="/absolute/independent-venv/bin/python"
export EVIDENCE="/absolute/new-verification-directory"
export PATH="/absolute/verified-git-2.53.0/bin:$PATH"
unset PYTHONPATH
mkdir -p "$EVIDENCE"
cd "$SOURCE"
"$PY" -I -m pytest -q \
  tests/product_config/test_git_user_observation_verification.py \
  tests/product_config/test_git_observation_verification_recipe.py \
  --junitxml="$EVIDENCE/api-recipe.xml"
"$PY" -I -m pytest -q \
  tests/product_config/test_git_checkpoint_preparation.py \
  tests/product_config/test_git_checkpoint_preparation_digest.py \
  tests/product_config/test_git_delivery_source_verification.py \
  tests/product_config/test_git_user_observation.py \
  tests/product_config/test_git_user_observation_controls.py \
  tests/session/test_authenticated_history.py \
  --junitxml="$EVIDENCE/regression.xml"
"$PY" -I -m pytest -q tests/product_config/test_git_baseline.py \
  --junitxml="$EVIDENCE/baseline.xml"
"$PY" -I -m pytest -q \
  tests/product_config/test_git_observation_intent_flags_verification.py \
  --junitxml="$EVIDENCE/intent-debug.xml"
```

逐步确认退出码、完整XML及用例身份；任何失败保留原件并停止验收，不能仅看末尾输出。
测试使用临时Workspace和夹具Provider；不发送真实模型请求，不接受真实账号或业务数据。
