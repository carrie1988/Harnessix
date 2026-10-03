---
doc_type: validation-evidence
status: current
version: 2
code_revision: 007d2bd7c7f769616ec94641283274990b2acd66
owners: [core]
modules: [tools, evals, product_config, documentation]
related_adrs:
  - docs/adr/0062-local-first-v1-commercial-boundary.md
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_git.py
  - tests/evals/test_delivery.py
  - tests/governance/test_windows_git_first_failure_projection.py
  - tests/governance/test_windows_git_trace2_input_binding.py
  - tests/governance/test_installed_product_acceptance.py
  - tests/session
  - tests/agent/test_authenticated_store.py
supersedes: []
---

# 固定007d2bd后继完整回归与安装、原生步骤验证报告

## 1. 固定输入、目标与完整本地结果

候选`007d2bd7c7f769616ec94641283274990b2acd66`已发布。普通独立测试Git副本的4826件受管输入，
在完整回归开始前与该候选工作区逐件原字节核验相等；不含外来未跟踪目录或旧dist。
Git2.53.0、Python3.12.7、Darwin arm64，关闭cacheprovider及额外插件自动加载，使用原异步插件。

原完整pytest范围实际11266项：**11129通过、0失败、0错误、137跳过、退出码0**。
相比前一完整基线，原四项失败均已在当前完整选择中通过，新增六项真实对象及旧能力准入回归也执行。
前一11260项的4FAIL原件不重写；成绩不是从定向302项推算，完整JUnit及精确输入摘要单独保存。

本结果证明本机完整离线回归，不是实际模型编码质量、全部目标系统或商用验收。
137项跳过按原测试平台／条件执行，没有删除原选择器或降低阈值。

## 2. 当前Wheel的实际macOS源码外生命周期

当前Wheel SHA-256为`90bc5bb577b95ef0a30d8a48b63d0ac2af2f6b394267429690b6109c763a7a54`。
在新专用venv中按原锁依赖和完整哈希安装，以Python -I运行原
[`installed_product_acceptance.py`](../../../scripts/installed_product_acceptance.py)。
实际包和固定来源均核验479成员，源码目录不在导入路径。

Doctor、活跃Owner备份拒绝、完整备份恢复、原Key与根保留、同恢复ID不回退新状态、卸载后导入缺失、
同Wheel重装及原Thread可读全部通过；用户Workspace原字节保持。操作仅针对独立夹具与专用环境。
模型端点使用provider.invalid，模型请求0，`commercial_release=false`。

该专项不证明真实编码任务、不同版本升级、Beta、Windows／Linux本次安装或全部消费者OS。
不把较早beda980的三平台安装结果与当前Wheel拼成同候选整体验收。

## 3. 当前自动CI与原生验证分层

[Run37149652886](https://github.com/carrie1988/Harnessix/actions/runs/37149652886)绑定同一固定候选。
初始观察点和后继观察点分别保存，均不改写为终态。后继观察中整体仍为in_progress；
两个Linux Job已结束且失败，故即使Windows后继通过，也不能声明本次整体CI通过。

| 已核验层次 | 原步骤／Job事实 | 边界 |
|---|---|---|
| Windows材料 | 三组authenticated-raw、object-input、cas-reference均success | 不替代完整产品和消费者Windows |
| Windows核心前置 | 原NTFS事务／审批写链、Git读取／取消步骤success | 不据本次通过推定较早NTFS失败唯一根因 |
| Windows认证Session | 原`pytest tests/session tests/agent/test_authenticated_store.py`步骤success | 核验退出结论，未读取原始业务日志或宣称具体原生用例计数 |
| Windows重启与恢复 | 原产品重启、业务状态备份／恢复步骤success | 不扩大为完整Git业务备份或消费者Windows验收 |
| Windows后继 | 广泛原选择仍in_progress | 完整Windows未验收 |
| macOS工具 | coding-tools-macos Job success | 与本机全仓结果分开，仍不代替真实模型任务 |
| 容器／文档 | 两Job success | 不能外推当前用户Docker或业务结果 |
| Linux Python3.12／3.13完整回归 | 两Job原完整`Run uv run pytest`步骤均success | 功能回归与整个Job结论分开，不宣称具体原生用例计数 |
| Linux Python3.12／3.13门禁 | 两Job终态failure，唯一失败步骤均为`license_scan.py --check`；后继SBOM步骤skipped | 原许可失败保留，未执行步骤不能算通过 |

仅读取Run、Job和step元数据；没有读取CI原始业务日志、stderr或调试日志。
旧失败Run保持，当前候选是实质生产整改后的新运行，不是原样rerun。
原策略及十二件通知失败的实际边界见
[许可通知复核报告](../license-notice-review-boundary-2026-10-04-v1/README.md)。

## 4. 原件、源码与设计对应

- [完整结果](full-result.json)：固定来源、输入范围与实际完整计数。
- [安装原结果](installed-macos-result.json)：既有验收器原字节、来源及Wheel身份。
- [初始CI观察元数据](ci-observation.json)：原观察字节保留，不冒充终态。
- [后继CI观察元数据](ci-followup-observation.json)：明确新观察时间、Linux终态与Windows实际活动状态，不覆盖初始记录。
- [SHA256SUMS](SHA256SUMS)：四份结构化原件摘要。
- [完整对象身份设计](../../changes/m09-r4-git-diff-full-object-identity.md)与[前序限定回归报告](../git-diff-history-convergence-2026-10-04-v1/README.md)：根因、正式接口、失败和兼容边界。

私有交付目录保存完整JUnit、日志、4826输入摘要及Review Packet。公共安装原件不包含Key、
用户配置或Workspace内容；生成的私有State和Key不被读取或复制到交付manifest。

## 5. 商用边界与后继

后继需核验该Run剩余Windows Job终态及原生完整范围；不因长时间观察或期限推算自动重启或取消。
真实R3最近完整结果仍严格成功0/20、必需测试1/20，
两笔费用未决继续阻断模型请求；本次没有新增模型请求或修改费用规则。
默认完整Git／Backup v2、消费者系统、独立Beta、权利及同候选R1～R6仍开放。
本报告不把完整离线绿色、安装成功或单步骤success称为商用1.0完成。
