---
doc_type: validation-evidence
status: current
version: 1
code_revision: e8a0804986666edb613dee7b1c5fd6713777fc77
owners: [core]
modules: [delivery, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_repository_recipe.py
  - tests/delivery/test_git_repository_recipe_integration.py
  - tests/product_config/test_git_repository_observation.py
  - tests/product_config/test_git_repository_observation_failures.py
  - tests/product_config/test_git_delivery_process.py
  - tests/product_config/test_git_shared_process_digest.py
supersedes: []
---

# Git仓库共享配方与受控观察验证报告

## 1. 需求背景、设计目标与验收边界

[总体与详细设计](../../changes/m09-r4-git-repository-observation.md)将原同步领域的完整固定仓库检查收敛为唯一配方，
产品内部观察显式借用原Owner、Scope、Supervisor及计划库，消费宿主给出的正式命令计划和原批准。
正常原合同与固定查询顺序保持，畸形根报告、树条目及字段OID增加固定拒绝校验。
计划等待、执行和CPU解析共用原取消与绝对期限，入口及最终返回处理父Task取消，不调用同步后台线程。

本报告仅证明内部完整仓库观察与关联兼容回归。不完成A/T/D写配方、ProductPlan/Link、NativeBridge、
独立Commit产品接线或Backup2。绑定不是原子快照、Session MAC或业务成功证明。
A保持干净、T2仅prepared、D承担物化；商用R1—R6、真实编码质量、三平台业务消费者及有限Beta仍开放。

## 2. 固定输入与实际安装包

- 基线提交：`e8a0804986666edb613dee7b1c5fd6713777fc77`；全部验证输入见[source-inputs.json](source-inputs.json)。
- 单一发行包：`harnessix-1.0.0rc1-py3-none-any.whl`。
- Wheel SHA256：`12759b2cf118dfeb9eedea9a4e6705a8eaf984a3a2e9e02389636cd6587549b1`。
- 470个源码模块与Wheel逐字节相同；实际加载310个Harnessix模块，全部来自安装目录。
- 测试解释器以`-I`启动，未将候选的`src`加入导入路径；Git使用已验证的2.53运行时。
- 广泛治理、文档和Secret检查在普通Git clone执行，不读取其他未受管目录。

## 3. 测试与检查结果

| 检查 | 实际结果 |
|---|---|
| 实际安装包关联回归 | 523 PASS，0 FAIL/ERROR/SKIP，204.361秒 |
| 治理完整回归 | 1413 PASS，0 FAIL/ERROR/SKIP，37.931秒；最终归档统计见[result.json](result.json) |
| 共用配方独立测试 | 169 PASS，含31个真实Git用例与8个属性拒绝优先级控制 |
| 受控异步观察测试 | 87 PASS，真实原共享Plan/Lease/Owner结算及取消/期限/批准/宿主/流验真负例 |
| 实现摘要漂移 | recipe和观察adapter变更均使旧正式计划及批准失效，拒绝前不保存本次Plan或启动Lease |
| Native18离线准入 | 18条原路径保持，仅两项各四个字节叶刷新，其他16行与全部非来源字段精确不变 |
| 历史Schema | 272个原文件逐字节保持，无新Schema |
| 静态与结构 | Ruff、format、470模块Mypy、原可读性及结构政策通过 |
| 文档图形 | 三图实际渲染并检查像素布局；最终静态文档检查见结果文件 |
| Secret检查 | 固定规则覆盖仓库及该Wheel，零命中；不以规则检查证明不存在所有可能秘密 |

169和87项属于523项集合，早期155项回归也与最终集合重叠，不累加为独立测试总数。
1413项为不同范围的治理检查，不代替业务或Windows原生验收。

## 4. 失败证据、根因与闭环

1. 独立配方初始运行出现14项拒绝缺口：NUL、空/相对根，非法或无完整NUL尾的树条目，畸形attributes OID。
   保留初始红控制；复用原OID验证并补齐精确解析，不增加容量或期限，不放宽支持范围；14项全部复验通过。
2. 原属性逐条读取优先级保持。危险attributes先于后续LFS/submodule时，保留原首次拒绝；安全attributes仍继续检查后续控制面。
3. 异步比较夹具最初使用不同Runner，hooksPath导致配置摘要不同；改为借原相同Runner后比较完整绑定，而非删除配置字段。
4. Supervisor关闭测试原先依赖会被关闭过程清空的内存句柄；改为核验原持久Plan/Lease事实与停止状态。
5. 初始治理发现缺失新文档及四项冻结身份断言漂移。新增正式文档；历史角色按原固定提交字节核验，
   当前目录限定基线后的两项八叶变化并核验精确实际字节。原27 selectors、13 Hooks及20/45/240/300秒门禁不变。
6. 设计中Python代码块的format控制已修复；未扩大原格式例外或降低政策。

所有初始失败、后继运行、测试输入及单一发行包均保留在私有验证目录，不删除失败或发布测试正文。

## 5. 风险、限制与下一接线边界

- 仅在macOS原POSIX宿主执行该观察。Native18离线门禁不证明新增Windows消费者已实测。
- 原端口单命令busy不等于整组观察互斥；业务层仍须原Lease/Owner协调和效果前后复核。
- authorize是受信且应合作取消的计划提供方；不承诺不合作回调的硬实时终止，不遗留后台准备任务。
- 命令普通完整输出仍为原1MiB，未借8MiB材料能力放宽读取；超限整体拒绝，不消费前缀。
- ProductPlan/Link、真实A/T/D阶段的受控写配方、跨Store语义、业务备份恢复及独立Commit均继续既定设计。
- 百炼API测试调用为零，费用为0元；原预算及未决费用预留不变。

审查材料：[Review Packet](review-packet.json)、[结果](result.json)、[SHA256SUMS](SHA256SUMS)。
