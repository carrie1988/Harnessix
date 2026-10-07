---
doc_type: validation-evidence
status: current
version: 1
code_revision: ba6171f32e5575be727364d166001ca0619db6a0
owners: [core]
modules: [sdk, product_config, evals]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_scoped_runtime.py
supersedes: []
---

# 完整未整改后端基线评审包

- **接受**：实际76类350项执行，302 PASS、9 failure、39 error、0 skipped；原日志和全部失败标识保留。
- **安全范围**：423件输入前后摘要一致、物理副本独立；实际OS负控通过，无新权限、网络或模型请求。
- **拒绝推广**：不是全量通过、历史失败豁免、登录修复、受控Profile、完整原目录正文未变或Beta完成。
- **开放项**：29项Socket权限错误的夹具适用性；另19项失败/错误的根因；同条件前后完整失败集合比较。
- **证据入口**：[完整报告](README.md)、[事实](facts.json)、[任务](../../operations/pilot-tasks/001-login-password-protection.md)。
