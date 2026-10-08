---
doc_type: validation-evidence
status: current
version: 1
code_revision: bef1ab088d271bec205f07d9a6ec942514b0efa7
owners: [core]
modules: [agent, evals, models]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/agent/test_runtime_thread_lock.py
  - tests/evals/test_provider_reverification_v2.py
supersedes: []
---

# 独立复核资料包

## 候选及批准范围

实施源码 `bef1ab088d271bec205f07d9a6ec942514b0efa7`；
最终Wheel SHA `0ac0725bb1e9c28d7164cc8918663e060475d86137e88fc453c31caeee445d26`。
批准范围仅为封闭预算合同及原Runtime锁归属原语。R3／R4／商用仍OPEN／NO-GO。

## 必须核对的源码及证据

1. 原v1 actual模块Schema／序列化比较，新v2组合不能混入旧解释。
2. 管理快照在Owner进入前拒绝实际类型／嵌套绕过；私有文件共用模型不能登记。
3. 全旧前缀、原未知和全额预留保持，新未知七模式仍停机，双限及绑定链不扩额。
4. 原Runtime字典中的精确锁与实际Task身份；未登记检查不建锁，非持有者不能释放。
5. 等待取消／超时、原异常实例和真实审批关闭；不是mock `locked()`正控。
6. 候选555生产成员三方字节一致，安装导入位于非editable site-packages。
7. JUnit终态、集合交集及四项初始fixture ERROR保留，不跳过原历史兼容测试。

## 未批准事项

没有实际Ledger注册、模型请求或费用结算；没有新Beta接受或客户代码整改。
没有原Git全程锁／FD／末端一致性／P1／正式决定Writer／默认完整交付／三平台验收。
不批准删除Checkpoint／Commit或改低R3质量阈值。

详见[完整报告](README.md)、[机器事实](facts.json)及[公开manifest](manifest.json)。
