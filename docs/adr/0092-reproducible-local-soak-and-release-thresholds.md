---
doc_type: adr
status: reviewing
version: 10
code_revision: 9d0e337f8078bdf0ecf9b9dd128ac3340eac6acf
owners:
  - core
modules:
  - agent
  - app_server
  - sdk
  - session
  - artifacts
  - trusted_actions
  - product_config
  - documentation
related_adrs:
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0089-bounded-local-transport-lifecycle.md
  - docs/adr/0090-plan-first-store-maintenance-and-backup.md
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
related_tests:
  - tests/agent/test_runtime.py
  - tests/app_server/test_server_sdk.py
  - tests/agent/test_store_maintenance.py
  - tests/product_config/test_action_recovery.py
  - tests/benchmarks/test_soak_long_session.py
  - tests/benchmarks/test_soak_many_threads.py
  - tests/benchmarks/test_soak_attempt.py
  - tests/benchmarks/test_soak_context_proof.py
supersedes: []
---

# ADR-0092：以独立负载、可重算证据和预先冻结阈值验收本地Coding Agent长期运行

## 状态

提议，待0.9.3d全部正式场景、三平台真实运行与独立阈值复验后接受。当前已实现低敏样本、Manifest合同、最后提交标记，以及长会话和多Thread真实模块Runner；两个Runner已在负载前保留Attempt开始，异常留存失败终态，硬退出留存未完成事实。缩小负载只产生`unverified`。[macOS 500 Thread单次诊断事实](../validation/soak-macos-2026-09-23-v1/README.md)已归档，但其Revision跨平台CI失败，不能用于冻结Profile；后续修复已由[CI 35804642232](https://github.com/carrie1988/Harnessix/actions/runs/35804642232)六实例通过，不改变旧证据属性。[macOS 1000 Turn长会话规模诊断](../validation/soak-macos-2026-09-23-v2/README.md)的Revision六实例CI通过，复制件Run/Attempt可重算，但没有专门Context/Compaction断言或独立阈值复验，仍不用于冻结Profile。现已新增[版本化Context/Compaction Proof](../changes/m09-3d-long-session-context-proof.md)及缩小负载Runner，旧v1证据保持不变；v2三平台正式负载尚未执行。其余四个场景和完整三平台性能证据仍未完成。本文不表示当前版本已经通过Soak或达到发布阈值。

## 背景

0.9.3a～c已经分别加固本地传输、共库保留、Action效果恢复，但局部故障测试不能证明大量Thread、长历史、
Artifact增长和反复重启时仍有可接受的启动时延、内存和恢复结果。[可靠性专项源码研究](../research/reliability-and-performance.md#10-093d长会话与性能证据专项源码核查)
确认当前启动和列表存在全量遍历路径；这些是**待实测风险**，不是已证实性能缺陷。参考项目的固定Benchmark目标、
恰好一次报告及预热/测量分离做法可借鉴，但不能替代Harnessix自己的真实产品链验收。

总设计已规定至少1000 Turn、500 Thread，以及SDK、Artifact、Action故障和重启场景；仅保存一次运行的P95，
或在看到结果后选择阈值，均不能形成可审计发布结论。

## 决策驱动因素

1. 负载必须调用当前Agent/SDK/Store/Trusted Action真实入口，不构建第二套假Runtime；
2. 基线与阈值验证必须是两次独立运行，不能用同一份样本既选阈值又证明达标；
3. 原始数值样本、环境和场景身份必须可重算、可校验，缺失或篡改失败关闭；
4. 不发布Prompt、代码、路径、业务ID、Secret或stderr，性能证据不能成为泄漏旁路；
5. Linux、macOS、Windows的RSS和时钟适配应声明单位与采样限制，不能以零值代表不支持；
6. 规模风险的优化必须由实测触发，且不得牺牲完整性扫描、审批和UNKNOWN不重放语义；
7. 长Soak不能拖慢每次离线快速CI，但发布门禁必须保留独立三平台证据；
8. 0.9.3d不新增Action HTTP/Worker、远程数据库或性能控制面服务。

## 候选方案

| 方案 | 收益 | 主要缺陷 | 结论 |
|---|---|---|---|
| 仅增加短时Microbenchmark | 快速、低成本 | 不覆盖增长、重启、故障和产品主链 | 拒绝 |
| 在每次普通CI运行完整长Soak | 自动化强 | 时长及平台资源波动影响常规提交反馈 | 拒绝 |
| 先重构全量扫描和分页 | 可能改善规模 | 尚无测量证据，容易改变恢复与列表语义 | 拒绝 |
| 固定独立负载、低敏数值证据、基线与阈值分离 | 可复现、可归因、保留产品边界 | 需要维护场景版本和三平台固定环境 | 采用 |

## 决策

1. 建立仓库内**手动/发布专用**Soak入口，使用临时Workspace和独立State Root；默认使用不保留完整ModelRequest/Prompt历史的确定性Provider，不需要网络或模型API Key。现有`ScriptedProvider.requests`会累计深拷贝请求，不适合作为长会话内存基线。
2. 场景版本固定为`harnessix.soak-scenario/v1`，至少覆盖长会话、多Thread分页与恢复、SDK容量/取消、Artifact增长与清理、Action故障恢复及冷/热重启。各场景使用真实产品模块，Manifest必须标识实际测量边界；替身只用于模型和受控故障目标。核心Agent/Session直连指标不得冒充完整产品启动指标，后者必须经过当前`run_product_stdio`组合根。
3. 证据合同分为数值样本、运行Manifest和阈值Profile三个版本化对象。Manifest记录代码Revision、平台/Python、CPU/内存档位、场景/种子、负载量、样本数、分位数、RSS、前后文件水位、故障计数和证据SHA-256。禁止自由文本字段及本机路径。
4. 预热与正式采样分离；正式样本按确定算法重算P50/P95/P99，缺样本、非有限数值、重复场景、未知字段、摘要不符或运行中断均不可判PASS。
5. **第一次运行只冻结事实基线**，不声明性能达标。工程阈值必须写入独立、带来源基线摘要的Profile，并在后续独立运行中验证；失败结果不可选择性删除或覆盖。
6. 校验器不信任Manifest中的自报分位数或PASS：先校验原始样本和哈希，再重算、核对环境与场景身份，最后比较阈值。平台缺测结果只能是`unverified`，不可填零或跨平台套用。
7. 性能超过阈值时先定位场景和源码热路径，再单独设计优化；不得仅加大超时、减少扫描对象或跳过UNKNOWN检查。
8. 0.9.3d完成须有三平台正式负载、独立复验、故障/隐私/篡改回归和完整文档；只有轻量合同测试的实现仍保持未完成。

## 理由

独立证据链可把“某机器跑得快”转化为可重算、可比较、可失败关闭的发布判断，同时保留快速CI反馈。
它先让全量遍历风险可测，再针对被证实的瓶颈改变实现，避免为性能提前破坏恢复完整性。低敏白名单比事后脱敏
更适合含真实Workspace/Agent历史的工具；默认确定性Provider使本地指标不受模型网络波动和费用影响。

## 后果

### 正面后果

- 发布性能结论有独立基线、阈值、原始数值、环境和三平台证据；
- 规模回归能定位到启动、列表、SDK、Artifact或Action恢复，不以单一总耗时掩盖；
- 现有Agent Protocol、Session Schema和Trusted Action效果语义不因采样改变。

### 负面后果与债务

- 固定发布环境、真实长Soak和多次独立运行增加验收时长；
- 若500/5000 Thread或长历史触发真实瓶颈，分页/增量恢复需另立兼容设计与回归，不在本文预先承诺；
- 0.9.4～0.9.6和1.0发行物未完成时，本ADR的Soak通过不等于产品正式商用完成。

## 兼容、安全与运维影响

Soak入口仅面向开发与发布，不加入CLI/SDK公共产品协议；不改变数据库Schema或用户数据。发布证据使用排他创建的
唯一Run目录，先写入并校验样本和Manifest，最后以原子替换写入固定文件`COMMITTED.json`。标记只含版本和Manifest的SHA-256，
不纳入Manifest自身摘要，避免循环引用。Validator只接受有效提交标记，
不依赖跨平台“目录整体原子重命名”或覆盖既有运行。受控测试只使用临时Workspace；故障注入不得触碰用户仓库、
公网Git或外部凭据。两个现有Runner先写`STARTED.json`，异常、取消或超时写低敏`FINAL.json/failed`并返回非零；硬退出保留未终结开始事实。`COMMITTED.json`之后、Attempt成功终态之前的崩溃仍不得判PASS。其余四场景尚未接入Attempt合同，不把不完整样本判定为通过。

## 验证方式

1. 合同测试覆盖缺字段、未知字段、NaN、负数、重复场景、摘要不符、分位数伪报、环境错配和阈值来源错误；
2. 快速CI运行缩小负载的同一代码路径，验证真实Agent/SDK/Store和失败注入，不把结果当正式性能基线；
3. 固定Linux/macOS/Windows环境分别完成正式负载与独立阈值复验，并保存可重算低敏证据；
4. 手动检查样本文件、Manifest和报告中不存在绝对路径、Prompt、代码、Tool正文、业务ID或凭据；
5. 若优化全量扫描，复跑0.9.3c Owner、Plan/Route/Artifact孤儿、UNKNOWN和零重复效果故障回归。

## 关联资料

| 类型 | 路径 | 关系 |
|---|---|---|
| 源码研究 | [可靠性专项研究](../research/reliability-and-performance.md) | 固定源码和待测风险 |
| 总体设计 | [0.9.3详细设计](../changes/m09-3-reliability-and-performance.md) | 场景和完成边界 |
| 模块设计 | [Agent](../modules/agent.md)、[Trusted Actions](../modules/trusted-actions.md) | 当前运行事实 |
| 现有测试 | [Runtime测试](../../tests/agent/test_runtime.py)、[恢复测试](../../tests/product_config/test_action_recovery.py) | 真实入口和故障语义 |

## 被取代关系

无。后续若改变场景或证据Schema，保留旧Profile与运行原件，并以新版本和新ADR解释差异。

## 变更记录

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| 1 | 2026-09-22 | 建立独立基线、阈值复验与三平台Soak决策。 |
| 2 | 2026-09-22 | 明确排他Run目录和末尾提交标记；不依赖跨平台目录原子重命名。 |
| 3 | 2026-09-23 | 同步样本/Manifest合同、Run目录提交标记与独立复核的局部实现；正式Soak及阈值验收仍未完成。 |
| 4 | 2026-09-23 | 两个已实现Runner在负载前持久写入Attempt开始，异常写失败终态，硬退出留未完成事实；明确Run已发布而Attempt未终结不得判PASS，其他四场景与Profile仍待实现。 |
| 5 | 2026-09-23 | 登记macOS单Thread连续1000 Turn诊断事实和Run/Attempt双重重算；明确规模完成不等于Context/Compaction、Threshold Profile或三平台发布门禁完成。 |
| 6 | 2026-09-23 | 为长会话Context/Compaction新增v2低敏事件Proof与Manifest版本，保留v1历史Run逐字节可读；缩小负载可核验，不提前接受三平台发布结论。 |
