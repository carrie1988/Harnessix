---
doc_type: validation-evidence
status: draft
version: 1
code_revision: pending
owners:
  - evals
  - security
modules:
  - evals
related_adrs:
  - docs/adr/0103-authenticated-sqlite-session-commit.md
  - docs/adr/0104-managed-session-key-and-default-root.md
related_tests:
  - tests/evals/test_task_pack_publication.py
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_task_pack_evidence_stop.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_provider_suite_cli.py
supersedes: []
---

# Eval 认证接线集成审查清单

## 1. 源码身份与范围

基线为 `dfba34e707ed845f3e9d844461e124015c22dca7`；源码未提交，精确实现以 `source-sha256.json` 和 `owned.diff` 为准。主线发布版本与独立接线基线分别记录，不对脏工作树执行 reset、stash 或 rebase。集成负责人应按限定差异应用，不能覆盖其他模块修改。

允许源仅为 Trial、Case、Suite、单责 publication 装配及预算入口同源 Scope 转发。新增测试、SDD 和离线验证包随同审查；Session、Key 后端、Schema、费用、Pack、Profile、Grader 均没有实现变更。

## 2. 必查契约

- [ ] 物化前取得原 Run Root Owner，已有 Root 原 FD 贯穿物化，执行和报告复核同一 Owner。
- [ ] 三路径共享同一 Binding、Session、Artifact 和 Scope；前缀恢复仍传入原显式 Scope。
- [ ] 恢复只通过 `original_key(PrivateStateTree)` 加载原材料，缺 Key 不 create/fallback。
- [ ] legacy Header、Seal 缺失和内容篡改先于 Provider 拒绝；旧夹具静止文件 SHA 保持。
- [ ] 默认 Suite 指定凭据冻结为非空 Scope；自定义 Factory 明确提供同源非空 Scope，不窥探私有字段。
- [ ] 受托 Key 线程取消／超时后结算唯一任务，清零迟到材料，释放锁。
- [ ] 预算脚本除 Scope import／with／参数转发之外 AST 与基线等价；费用、Guard、模型参数未改。

## 3. 集成测试与阻塞项

独立结果为 **114 passed / 1 xfailed**，不是完整认证验收。`test_completed_recovery_requires_original_event_body_mac` 的严格预期失败必须在 Session 同事务读缝集成后移除，并在集成精确源码上真实通过。不能将 xfailed 统计为 PASS，不能保留标记规避 XPASS。

集成后必须重跑五个 Eval 测试文件和受影响 Session／Artifact 测试；保留解释器来源、源码 SHA、实际命令与结果。Ruff、Mypy、文档检查分别有独立记录。离线 SQLite 和录制事实不替代真实 Provider 编码质量或 Windows 原生验收。

## 4. 发布与回退边界

- [ ] 集成提交产生后更新 SDD／验证文档 `code_revision`，完成评审前保持 draft/pending。
- [ ] 同步现行 `docs/modules/evals.md`，消除原文档变更门禁的 `doc_sync_module_required`；不把本包三文档局部通过等同于全库门禁通过。
- [ ] 认证 Reader 闭环之前不得启动新的真实 Suite 验收。
- [ ] 不补签实际旧 Run，不生成旧 Key，不改变任何未决费用状态或预留。
- [ ] 回退代码不能删除新 Run Key，不能将新认证库降级为 legacy。
- [ ] 源包以独立 Manifest 校验；临时 Key、SQLite、Scope 材料不纳入交付。

## 5. 设计与证据

[完整 SDD](../../changes/m09-r3-eval-publication-wiring.md)；[实际验证报告](REPORT.md)。结构化来源／范围／图检查／封存见同目录 JSON，精确文件摘要见 `source-sha256.json`。
