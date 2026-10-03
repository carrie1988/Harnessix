---
doc_type: validation-evidence
status: current
version: 1
code_revision: 10d58c597f08d81e4201879e1b9c70fd37454aa6
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_cas_integration.py
  - tests/governance/test_windows_git_native_process_arming.py
supersedes: []
---

# 创建事件候选的固定原生结果

固定`10d58c597f08d81e4201879e1b9c70fd37454aa6`的[Run37080708999](https://github.com/carrie1988/Harnessix/actions/runs/37080708999)，attempt1、Job111080374339终态FAIL，没有重跑。候选设计见[主映像创建事件布防](../../changes/m09-r4-git-native-process-arming.md)。

原十六件输入和selected两对PE/PDB通过，现场CDB/Python存在且启动。结果仍为EXECUTION_INCOMPLETE；A/B各自为Git128、Worker2、proof ABSENT、call failed，操作未正常返回。三个marker语法计数仍为0，原分支见证不完整，原SDK验收false，历史根因UNKNOWN。

该实际结果表明：事件入口调整没有在此次现场取得完整见证或消除Git失败。不能把源码入口合同和235项离线通过解释成原生成功，也不能由0匹配推出没有创建Git或唯一CDB故障。应先求证现有脚本执行与材料失败接缝，不能重复该候选运行。

原精确两文件、Windows回执CRLF、API ZIP摘要已核对；[SUMMARY.md](SUMMARY.md)给出详细事实，[download-verification.json](download-verification.json)固定原字节验证，[review.json](review.json)限定结论，[manifest.json](manifest.json)绑定本包与固定源。此前Run37077033494及其他FAIL不覆盖。

完整Git默认交付/Backup v2、真实R3、消费者Windows、Beta及同候选R1～R6仍开放；本次没有模型请求。
