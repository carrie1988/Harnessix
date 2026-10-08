---
doc_type: validation-evidence
status: current
version: 1
code_revision: d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2
owners: [core]
modules: [agent, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_prepared_runtime_thread.py
  - tests/agent/test_runtime_thread_lock_observer.py
supersedes: []
---

# Git Runtime Thread 绑定复核包

## 范围及判断

仅准入内部组件正控；完整回归、B7、B4、P1、R3／R4／商用发布保持 OPEN／NO-GO。
默认产品未注册新 Git Writer。不得将纯替身测试解释为实际授权来源。

## 必查

1. 原 Runtime／Session／Gateway／Router／Artifact identity 与原 lock bound method。
2. acquire 独立代际、原 Task 当前准入、子 Task 观察不升级为 SQL／release 权限。
3. 原连接活动 context、已有事务、目标 Thread 与全集认证不筛行。
4. 原失败记录、两项原审批期限失败、未终态的 consumer／复核状态。
5. 556 个生产成员 source／Wheel／installed／Git 字节一致；JUnit 与原锁重复子集不得累加。
6. caller 窗口覆盖 COMMIT／ROLLBACK，不把尾部检查称为裸 SQL 拦截器或已提交补偿。

## 交付原件

核验 README、facts、verification 和 manifest；三幅 SVG 与详细设计相互对应。
私有清单不得包含凭据，历史封存不得修改。新结果须建立新版本，不覆写本固定快照。
