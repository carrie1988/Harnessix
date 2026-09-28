---
doc_type: validation-evidence
status: current
version: 1
code_revision: 90de93f565ea88679e54242ee6f1771e9be721b7
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_verification_budget.py
  - tests/evals/test_provider_verification_host.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_suite_execution.py
  - tests/evals/test_task_pack_execution.py
supersedes: []
---

# R3 有限验证请求预算、取消与正式Runner接线验证报告

## 1. 摘要、设计目标与固定版本

验证宿主已增加原预算Owner、发送前持久预留、官方Adapter请求Guard和正式Suite/Case恢复绑定。
请求费用未知时停止整个Suite并保留占用；可靠结算先于成功终态发布。
不创建或重置预算，不改变Task Pack、评分器、公开Schema、依赖锁或默认Factory身份。

完整需求、架构、流程/时序/数据流、接口、字段、核心伪代码、故障恢复及安全/部署边界见
[总体与详细设计](../../changes/m09-r3-verification-request-budget.md)。
本目录集中保存报告、结构化事实、Review Packet、日志摘录、实际渲染图与Manifest。

| Revision | 变化 | 验证边界 |
|---|---|---|
| `7b4219a15fe474c1721579fc30839a4206494036` | 请求账本、Guard、宿主和双层Factory绑定 | 两个Python版本受影响回归、实际镜像前置拒绝及Wheel源码匹配 |
| `90de93f565ea88679e54242ee6f1771e9be721b7` | 仅将离线Adapter测试假值改为明确非凭据Canary | 两版本65项最终专项、固定规则扫描；src及scripts原字节与前版本相同 |

## 2. 系统架构、核心流程与数据边界

验证宿主先检查固定模型/价格、源码Revision、Git/Engine和原Pack镜像；随后独占原账本、读取短生命周期凭据，
把Guard Factory交给既有Suite Runner。唯一Agent使用同一Provider进行主请求及摘要，工具、审批、评分和恢复不旁路。

每请求先持久预留，再消费实际Adapter；Attempt、Usage、原模型和终态一致才能结算并发布成功。
Suite取消通过现有取消机制回收迭代子任务；取消、流截断和未知费用保留占用，重启不自动退款。
完整已知零与未发送请求可以释放预留，但缺失/部分用量不是零。

账本原周期、原额度和历史不改写；只记录用途、模型、运行身份和费用事实，不保存Key、Prompt、参数、正文或代码。
最高档预留及估算只约束受控合作验证宿主，不等于供应商账户硬停止或最终账单。
配置、Owner和协议细节见详设；本报告不复制私有账本或用户凭据配置。

## 3. 执行结果与环境

| 验证 | 环境/版本 | 结果 |
|---|---|---|
| 受托Factory绑定红测试 | 改动前默认实现 | 3失败、3未选，0.67秒；缺少注入参数 |
| Suite独立取消红测试 | 仅入口检查Suite取消的Guard | 1失败、26未选，1.37秒；阻塞IO直到测试超时 |
| 取消及预留修复专项 | Guard复用`CancelToken.run` | 10通过、17未选，0.28秒 |
| 最终Guard/宿主/Factory专项 | `90de93f`、macOS ARM64 Python3.13.8 | 65通过，0.97秒 |
| 受影响模块回归 | `7b4219a`、macOS ARM64 Python3.13.8 | 2668通过、13跳过，189.57秒 |
| 独立受影响模块回归 | `7b4219a`、干净工作树Python3.12.7 | 2668通过、13跳过，193.27秒 |
| 独立最终专项 | `90de93f`、干净工作树Python3.12.7 | 65通过，1.19秒 |
| 静态与合同 | 固定生产实现 | Ruff、364源码文件与3脚本的Mypy、Schema/Pack及可读性检查通过 |
| 文档与治理 | 最终文档候选 | 29项治理测试及文档静态门禁；详见结构化验证及日志 |
| 图示 | 总体架构、请求时序、数据流 | 三幅实际渲染并视觉检查，源与PNG在本目录 |

回归组互有重叠，不能相加作为全仓通过数。生产代码相同证明只用于复用受影响回归，不把旧版本测试写成新版本执行。
本次没有运行全仓、Windows原生或Linux实际宿主验收，也没有关闭R1～R6。

### 3.1 故障、边界与集成验证

- 同一原账本第二Owner拒绝；权限、链接、原字节漂移、周期、重复JSON和金额不一致均不自动修复。
- 四个文件/目录同步故障点覆盖预留及结算前后；失败不发布成功、不继续请求，自有临时文件清理。
- 缺失/部分用量、模型别名、输出超限、重试标记、终态后事件、Suite取消和父Task退出保留未决占用。
- 六个价格档位边界使用定点整数；输入/输出上限与Bool反例拒绝，不把价格估算当作账单。
- 实际OpenAI SDK与Adapter经过MockTransport，证明预留先于HTTP Transport、结算先于成功、源流关闭。
- 宿主禁网早于配置/预算/凭据读取；范围/价格/源码/镜像拒绝先于凭据；显式钥匙串失败不回退。
- 默认Factory指纹兼容；受托摘要同时进入Case及Suite，并复用原漂移恢复测试。

## 4. 实际宿主预检与费用事实

[`host-preflight.json`](host-preflight.json)记录干净独立Python3.12.7工作树实际运行宿主：
固定源码及程序校验通过后，原Pack镜像缺失，返回`verification_image_unavailable`和退出码1。
原预算未读取，预算文件/Owner锁未创建；没有自动拉取、替换Digest或改变Docker配置。

**新增百炼请求及费用均为0。** 未重置或写入原预算。未启动整改后的完整真实Suite，
历史[0/20完整基线](../../validation/provider-engineering-2026-09-20-v1/README.md)保持原样。
镜像前置拒绝证明停止边界，不证明Docker EOF的排他根因、百炼线上认证或编码质量。

## 5. 发行物扫描与原失败保留

构建0.1.0验证Wheel并核对`provider_suite_execution.py`与固定源码逐字节一致；
验证宿主脚本和测试不进入Wheel。摘要及边界见[`wheel-observation.json`](wheel-observation.json)，
这不是三平台正式安装、升级或1.0发行验收。

首次源码仓与Wheel联合扫描命中一处24字符的离线测试假值。改用既有明确非凭据Canary后，
固定六规则自检及联合扫描通过，2507个当次输入完整覆盖，未修改扫描规则或白名单。
源码扫描只使用Git受管输入；未读取、运行、stage、打包或计入用户未受管测试资料。
最终资料新增后会执行新的联合扫描，其输入数不与2507旧批次混写。

可读性报告初次检查因实际源码行数增加而漂移，更新正式统计后原策略通过；没有放宽阈值。
红测试、扫描及报告漂移均保留于日志，不覆盖为绿色。故障注入不等于真实断电实验。

## 6. 资料清单、复核方式与源码位置

| 文件 | 内容 |
|---|---|
| [`contract-facts.json`](contract-facts.json) | 固定合同、额度/请求边界、生产字节保持及未关闭质量声明 |
| [`verification.json`](verification.json) | 精确版本/环境/测试计数、日志位置和重叠范围 |
| [`host-preflight.json`](host-preflight.json) | 实际宿主拒绝及零请求证据 |
| [`wheel-observation.json`](wheel-observation.json) | Wheel摘要、源码字节核对及发行边界 |
| [`review-packet.md`](review-packet.md) | 审查重点、风险、必读路径和退出条件 |
| [`bundle-manifest.json`](bundle-manifest.json) | 除Manifest自身外全部资料字节数及SHA-256 |

核心源码为[`provider_suite_execution.py`](../../../src/harnessix/evals/provider_suite_execution.py)、
[`provider_verification_budget.py`](../../../scripts/provider_verification_budget.py)、
[`provider_verification_guard.py`](../../../scripts/provider_verification_guard.py)及
[`run_engineering_provider_suite_budgeted.py`](../../../scripts/run_engineering_provider_suite_budgeted.py)。
正式操作与停止恢复流程见[运维手册](../../operations/provider-suite-baseline.md)。
复核须按Manifest精确文件集重算SHA；日志仅保存本次低敏摘录，不复制私有配置、预算、运行状态、Workspace或Artifact。

## 7. 发布风险与下一步

固定镜像仍未就绪；不得改Pack Digest、选Case、降阈值或用离线Provider代替正式真实结果。
费用未知时必须外部受控核对，不自动退款后继续。Windows验证宿主账本不在本切片范围，
但Windows原生产品编码承诺仍由R4完成，不能以本POSIX脚本或历史只读链豁免。

完整产品多Store/Artifact与原Key备份恢复、12件许可处置、Windows原生写入、三平台发行、
独立Beta和最终发布仍开放。预算接线及本报告不关闭真实质量、商用安全或1.0门禁。
