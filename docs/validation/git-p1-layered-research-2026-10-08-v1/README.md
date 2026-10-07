---
doc_type: validation-evidence
status: current
version: 1
code_revision: ba6171f32e5575be727364d166001ca0619db6a0
owners: [core]
modules: [product_config, delivery, session]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_prepared_link_controls.py
  - tests/product_config/test_git_prepared_link_terminal_callbacks.py
supersedes: []
---

# Git P1 分层控制研究的真实离线SDK复验

## 1. 结论与范围

`RESEARCH_HOLD_P1_OPEN`。私有默认关闭研究桥的纯段代次撤销已通过短测；本次以相同四件研究源码
执行真实原认证SDK链，不复用此前性能数据。3项整链及15项短测通过，仍不可合并或发布。
未改变正式产品、原审批/单调期限、认证强度、Writer或效果能力。

## 2. 源码与候选边界

研究基于`61c91ce2e254dcc8ed582c924eb44a2bd0cdc123`，与v3研究源码摘要一致。
主审确认该基线到`ba6171f32e5575be727364d166001ca0619db6a0`的已跟踪`src/`无差异；
这不是全部依赖、测试、装配及兼容性证明，也不表明四件研究改动可以直接合入。

研究仅在操作局部纯数据编解码/build中分层，进入/离段和实际I/O、发布、终端仍保持原full检查。
每纯段独立可撤销代次，同任务/线程绑定；保存的callback离段、异常、嵌套或复制上下文后回到full。
详见正式[prepared-link设计](../../changes/m09-r4-git-prepared-link.md)中的原控制合同；研究不替代现行实现。

## 3. 实际结果与原期限

| 阶段 | 实际观察 | 验收边界 |
|---|---|---|
| 原prepared发布 | 32.03秒，GitDB写计数增加4 | 是预期发布，不称全程只读；不是approved Writer |
| 完整pending-read | 18.14秒，`total_changes=0`，前后MACHASH相同 | 原认证、全集回读及固定状态样本；不是任意规模SLA |
| 物理Owner替换/取消/异常 | 原固定拒绝与异常身份通过 | 注入在consumer首个full callback，不覆盖所有时窗 |
| 原60秒consumer期限 | 实际等待后拒绝 | 不改deadline/clock；不是120秒审批expiry或真实慢I/O证明 |
| 生命周期短测 | 15 PASS | 不与3项整链混称完整SDK或全部控制矩阵 |

整链3项总242.51秒；最大读取心跳间隔约5.25秒，仍存在同步阻塞。
同机Maven基线并行前提已登记，无本轮配对baseline，不声称稳态SLA或相对此前样本的统计改善。

## 4. 失败、资源与安全缺口

旧静态失败、证据提取失败及撤销红测11 FAIL/3 PASS原件保留。
本轮FD诊断连接未显式关闭，计数受诊断污染；不能判断生产泄漏，也不能认定资源门禁通过。
MACHASH是受审状态行摘要，不独立替代原MAC认证；真实只读额外以原全库/CAS/Git快照及写计数核对。

纯段内外部callback频次和瞬时漂移检测时点会改变，不能宣称与原逐叶full等价。
callback可观察性、失败优先顺序、纯段时长上限和检测SLA合同未决；完整SDK、实际I/O/发布后故障窗口仍未闭合。
禁止因少数正反例通过默认启用研究桥或放宽原安全/期限规则。

## 5. 证据与后续

私有31件Manifest封存原命令、JUnit、计数/心跳、源绑定、完整patch、原边界摘录及评审；
主审逐件核验摘要。[事实](facts.json)、[评审包](REVIEW_PACKET.md)只发布必要元数据。
原模型/Keychain/预算未访问，主仓生产源码未变；P1、approved Writer、B4/B7及R1～R6保持开放。
后续先解决控制合同及完整适用回归，修正诊断连接关闭，再采样资源和期限；不带研究数据发布1.0。
