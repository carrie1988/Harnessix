---
doc_type: validation-evidence
status: current
version: 1
code_revision: dc1953acc92e201714a3fa9a8bc6ad3032b40a2b
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/governance/test_windows_git_native_process_arming.py
supersedes: []
---

# 有限验证命令

使用锁定开发环境，在仓库根设置`PYTHONPATH=src:.`和`PYTHONDONTWRITEBYTECODE=1`；pytest私有输出置于仓库之外。

```bash
python -B -m pytest -q \
  tests/governance/test_windows_git_native_branch_observation.py \
  tests/governance/test_windows_git_native_branch_preflight_v2.py \
  tests/governance/test_windows_git_native_selected_layout.py \
  tests/governance/test_windows_git_native_execution_v3.py \
  tests/governance/test_windows_git_native_process_arming.py
python -m ruff check scripts/windows_git_native_branch_observation/preflight.py \
  tests/governance/test_windows_git_native_process_arming.py
python -m ruff format --check scripts/windows_git_native_branch_observation/preflight.py \
  tests/governance/test_windows_git_native_process_arming.py
python scripts/documentation_check.py --changed-from dc1953acc92e201714a3fa9a8bc6ad3032b40a2b
```

Windows现场只能使用原`windows-git-native-branch-observation.yml`手动入口，以完整新提交作`expected_revision`，`execute=true`且attempt1。未改变候选不重复运行；结果失败照实归档，不当成功验收。
