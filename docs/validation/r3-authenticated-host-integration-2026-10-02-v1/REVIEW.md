---
doc_type: validation-evidence
status: draft
version: 2
code_revision: pending
owners: [evals, security]
modules: [evals, product_config, session, artifacts, secrets]
related_adrs:
  - docs/adr/0095-versioned-secret-publication-scope.md
  - docs/adr/0103-authenticated-sqlite-session-commit.md
  - docs/adr/0104-managed-session-key-and-default-root.md
  - docs/adr/0107-authenticated-eval-host-and-history-read.md
related_tests:
  - tests/evals/test_task_pack_publication.py
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_task_pack_evidence_stop.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_provider_suite_cli.py
supersedes: []
---

# R3 认证宿主完整历史消费：Review Packet

## 1. 候选定位与阅读入口

研究基线 80，集成基线 730，未提交源码以 [冻结八输入](owned-source-sha256.json) 标识。详设见 [SDD](../../changes/m09-r3-eval-publication-wiring.md)，结果见 [REPORT](REPORT.md)，正式 Session 事务见 [Session 设计](../../changes/m09-r4-authenticated-thread-history.md)。只包含指定 Eval／脚本／测试和文档，不修改 Session 文件、原 48 包或费用规则。

## 2. 必须复核的安全及兼容边界

- [x] 完成、终态、执行前门禁都消费完整 carrier；首／中间原事件 MAC 先于任何 Provider 或 Runtime 恢复。
- [x] 原 CancelToken、当前 Owner、一次绝对 120 秒 deadline 贯穿；前缀不按 Run 刷新。发现读也托管取消和剩余时间。
- [x] 新只读步骤与原 Turn.remaining_seconds 分离；终态原预算过期仍可只读验真，不授 execution 续期。
- [x] 原 Reader 10 秒上限与 Session 字节未变；Root／Binding close 及 Owner 原系统错误按原边界拒绝。
- [x] 恢复只读原 Key；缺 Key／旧 MAC 无 fallback、create 或补签；原真实 Run 未访问。
- [x] 同一 Run Owner／Binding／Scope；前缀重开借原稳定身份，不虚称跨对象相同。
- [x] `eval.provider/1` 不代表云端 Key 版本；不从 opaque Scope 摘要生成不存在的旧映射。
- [x] **新增取消兼容变化显式披露**：已取消非空 durable-stop prefix 传播 TurnCancelled；旧 v1 FAIL 保留，不将其称为夹具修正。
- [x] 该边界已断言原 Token、0 Provider、0 Trial 重放、0 Campaign 状态写、静止 prefix SHA 不变；未取消停止／费用优先级与空 prefix stop 返回不变。
- [x] 原 MAC strict xfail 已移除，133 真 PASS；Ruff／Mypy 和四图检查通过。
- [x] 十个保护输入、原 Manifest 与 48 成员 SHA 未变；只输出指定 owned diff。
- [ ] 独立审查者在最终精确 SHA 上复跑 selectors 并签字。
- [ ] 精确候选唯一 Wheel 安装及发布收口验收。

前述勾选为实现方的实际自检证据，不是独立审查者签字，不把离线认证 PASS 当真实 R3 或商用验收。

## 3. 精确复验入口

完整 selector：[selectors.json](selectors.json)。取消优先级单独复验：

```text
tests/evals/test_task_pack_evidence_stop.py::test_case_evidence_stop_is_durable_without_trial_replay
```

原事件缺口：

```text
tests/evals/test_task_pack_publication.py::test_completed_recovery_requires_original_event_body_mac
```

使用指定集成解释器、PYTHONPATH 仅集成树 src，按 REPORT 命令将输出写入新的私有目录。勿写原验证包、访问原 Key／Run／费用台账或发新请求。先校验八源 SHA 与当前 Session 输入身份，再解释测试结果。

## 4. 失败闭环与不能作出的推论

旧 v1 的 durable-stop 兼容失败永久保留；后续失败日志完整记录真实契约错用及新夹具构造边界的修复过程。最终读取控制不重置 Turn Budget，不移除难测恢复路径，不改变评分及预算规则。

本包未验证真实编码质量、Windows 原生、真实 Provider／费用入口、全仓门禁或安装。发现候选输入漂移应停止宣称同字节结果，重新冻结与复验，不能回滚其他工作树修改或改写历史 Manifest。


- [ ] 原 48 包的集成副本权限由归档负责方处置；当前成员 SHA 未变，但原完整 verifier 因目录权限 FAIL，见 [权限观察](original-package-permission-observation.json)。本切片不改原包。
