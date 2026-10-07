---
doc_type: validation-evidence
status: current
version: 1
code_revision: 82c95e677d1919c60bbb3be32a9a4ef23f35b2e4
owners: [core]
modules: [product_config, session, delivery, documentation]
related_adrs: [docs/adr/0068-transactional-workspace-and-git-delivery.md]
related_tests:
  - tests/product_config/test_git_user_observation_verification.py
  - tests/product_config/test_git_observation_intent_flags_verification.py
  - tests/product_config/test_git_checkpoint_preparation.py
  - tests/session/test_authenticated_history.py
supersedes: []
---

# 用户Git观察只读复核：评审资料包

## 1. 评审范围与结论

仅评审阶段无关U只读入口、原prepare的共享末轮配方、真实原生核验与控制/失败边界。
结论为 `READONLY_DEPENDENCY_VERIFIED_NOT_RELEASE`；实际安装核心345项通过，不等于B3决定闭包、Git效果或商用验收。
[完整设计](../../changes/m09-r4-git-user-observation.md#10-阶段无关的原观察只读复核)、[验证报告](README.md)、[结构化结论](result.json)及[运行历史](run-history.json)共同限定本结论。

## 2. 缺陷与回归证据

| 发现 | 真实原件及后置条件 |
|---|---|
| 原Reader/member可被公开摘要掩盖 | 四项修复digest/fingerprint后的DID NOT RAISE保留；补原Reader与_member实际验证后拒绝 |
| 原生Root不等于实际GitRoot | core.worktree重定向并修复配置/状态摘要的RED保留；补实际show-toplevel，父仓库负例同时通过 |
| before/Index、Reader操作期及结算组合缺测 | 实际before正文损坏、三类Index状态、六项Reader漂移、四项真实Session组合新增覆盖；全部包含在最终90项组中 |
| intent-to-add可能先在stage失败 | 首次静态复核明确限制；补独立空HEAD入口负例，先证明stage与-v匹配，再实证非零debug flags拒绝，安装组1项通过 |
| 验证宿主及资料缺口 | 保留未装包11FAIL、旧Git初始化63FAIL及治理资料2FAIL；修正环境/同步快照，不放宽产品策略 |

## 3. 独立静态评审与实测分离

两次独立只读静态评审未执行Git、测试或模型，不把测试源码存在当成通过。
初评提出实际GitRoot P2与组合缺测，后继静态复核确认代码边界闭合；
其debug分支精细限制随后由新增1项真实安装负控补齐，原评审不改写。
两份原评审及全部日志/XML以SHA封存在专用受控证据目录，公开报告仅给出低敏索引。

当前实现保持原MAC/Source2/实际资源、原绝对期限与Session结算优先级。
公开摘要不是认证，新入口不collect、不put_blob、不新增路由/批准/效果。
原pending-only prepare没有变成阶段无关，末轮先后顺序与原source/history注入点保持。

## 4. 安装与输入来源

548包成员/507Python源码在固定候选、Wheel及独立site-packages逐字节相等；
`python -I`和无PYTHONPATH确保SDK受管子进程使用实际安装包，见[成员证明](installed-content-proof.json)。
[1446原输入](source-inputs.json)及[1件新增测试](extra-test-inputs.json)分别冻结，产品源码未变化。
原可读性阈值及策略未改，仅同步源码现行快照；生成合同及文档治理另记，不混计345核心测试。

## 5. 开放门禁

B3全接线、B4/B7、approved Writer、NativeBridge、A/T2/D、Commit/Backup2与同步响应性P1开放。
R3实际模型质量/两笔费用未决、三平台消费者、独立Beta、同候选R1～R6不因本报告关闭。
BETA-001只登记，实际自主完成0；参考副本不喂入Agent也不算自动结果，原项目写入始终禁止。
未新增模型请求、读取真实凭据或生产部署，无Schema/DDL、依赖锁、授权、预算或工具广告变更。
