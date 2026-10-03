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

# 源码定位与调用链

| 位置 | 职责与边界 |
| --- | --- |
| [preflight.debugger_scripts](../../../scripts/windows_git_native_branch_observation/preflight.py) | 唯一修改：bootstrap创建事件入口；旧三个handler保持 |
| [observe](../../../scripts/windows_git_native_branch_observation/observe.py) | 原预检、CDB执行、完整门、有限结果及退出值，不改 |
| [projection](../../../scripts/windows_git_native_branch_observation/projection.py) | 原整行grammar、顺序和值域，不改 |
| [run_cases](../../../scripts/windows_git_native_branch_observation/run_cases.py) | 原两项selector与有限帧边界，不改 |
| [contract](../../../scripts/windows_git_native_branch_observation/contract.json) | 原固定PE/PDB、16输入及期限，不改 |
| [新回归](../../../tests/governance/test_windows_git_native_process_arming.py) | 入口、格式、guard与模板拒绝的8项离线证据 |

调用链：`observe.run → prepare → debugger_scripts → 原CDB → cpr:git.exe → 原布防脚本 → 原两个handler → observation_result → 原两文件发布`。

创建事件名只是回调筛选，不产生身份或执行权限。旧结果里0匹配不是没有创建进程的证明；新脚本文本通过不是实际断点命中的证明。
