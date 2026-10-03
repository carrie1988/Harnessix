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

# 固定Run事实与未证实边界

| 项目 | 实际结果 |
| --- | --- |
| Run / Attempt / Job | 37080708999 / 1 / 111080374339 |
| 完整提交 | 10d58c597f08d81e4201879e1b9c70fd37454aa6 |
| Workflow终态 | failure；安装锁定依赖和精确结果上传成功 |
| 观察状态 | EXECUTION_INCOMPLETE；execution_performed=true |
| 原输入 / PE与PDB | 16件；两对完整匹配 |
| debugger_exit / pytest_exit | 1 / 1 |
| 超时 / 日志超限 | false / false |
| A / B | 各call failed、Git128、Worker2、proof ABSENT、操作未正常返回 |
| 未认证形状 | FINITE_AB；source_case_report_invalid=false |
| ARM / BRANCH / bad-prefix完整语法数 | 0 / 0 / 0；marker_state=MEASURED |
| 完整分支见证 / SDK验收 | false / false |
| historical_root | UNKNOWN |
| Artifact | 11258242022；1378B；精确result.json和result-sha256.json |
| ZIP/API SHA256 | c0fffceb517e871160251814cb8ca998b440698a0eeda7c994808ceca51c38b7 |
| result.json | 2901B、LF、b5effba8273f49d21a7c718b5e23837b4b9fb42ab763a97a1bc0995d2b322498 |
| 摘要回执 | 87B、一处CRLF、26096086487ae6f0a36714d08a7e0e73eef809af92385152990ec07832b850f8 |

原v5 raw字段状态OBSERVED_V5_VERIFIED_NOT_INDEPENDENT_MAC不是下载方独立MAC验真；legacy cases与新观察均不授执行权。case映射为原顺序A/B，非Owner/PID认证。

候选只调整bootstrap的创建事件入口，原三个handler、固定PE/PDB、选择器、生产Git材料、PATH及期限不变。此次结果不确定回调是否执行、guard是否通过、日志是否含其他调试错误或Git唯一故障。公开Artifact不含私有CDB日志，不能声称已经读到不存在的原件。

两个结果文件以原字节保存，回执绑定`result_sha256`；ZIP与API摘要一致。没有文本换行转换、失败重跑、阈值/契约豁免、模型费用结算或商用门禁关闭。
