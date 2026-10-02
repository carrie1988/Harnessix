---
doc_type: validation-evidence
status: current
version: 1
code_revision: 6e440ffad07ee31ed6153838f858240f5754c876
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/product_config/test_git_material_cas_integration.py
  - tests/product_config/test_git_material_input.py
supersedes: []
---

# 固定v3原生结果：两案例失败投影已恢复

## 文档摘要

固定`6e440ffad07ee31ed6153838f858240f5754c876`的
[Run37077033494](https://github.com/carrie1988/Harnessix/actions/runs/37077033494)仅执行attempt1，
Job111069142609终态失败。原输入、选中映像、工具与执行控制不变；本结果不是原生功能验收通过。
观察器设计及离线发布检查见[详设](../../changes/m09-r4-git-native-execution-envelope-v3.md)和
[实现发布复核](../git-native-execution-envelope-2026-10-02-v3/PUBLICATION.md)。

## 精确内容

[run.json](run.json)、[jobs.json](jobs.json)、[artifacts.json](artifacts.json)是实际API响应；
[artifact.zip](artifact.zip)及[result.json](result.json)、[result-sha256.json](result-sha256.json)
保存原始下载字节。摘要核验见[download-verification.json](download-verification.json)，
限定结论见[SUMMARY.json](SUMMARY.json)、[review-packet.json](review-packet.json)，
全部发布输入由[manifest.json](manifest.json)逐字节绑定。

## 实际验证

- checkout、锁定依赖安装及结果上传通过，原十六件源输入与两对完整PE/PDB身份通过。
- 已启动调试执行，无超时和日志超限；debugger与pytest退出均为1，状态EXECUTION_INCOMPLETE。
- A/B均记录call failed、Worker返回2、Git返回128、proof ABSENT、original_operation_returned=false。
  两行均diagnostic_incomplete=true、diagnostic_truncated=false。
- 新观察为FINITE_AB、source_case_report_invalid=false；日志可读，三个marker语法计数均为0。
  原完整branch见证false、invocations为空；SDK验收仍false。
- Artifact11256673736精确1376字节，ZIP摘要与API digest相同；两文件回执匹配结果原始字节。
  结果为LF，摘要回执包含一处CRLF，保存和提交均不得转换。

## 安全与未验证

顶层cases与新增cases都不是独立MAC验真或业务执行授权。
OBSERVED_V5_VERIFIED_NOT_INDEPENDENT_MAC明确只表示原v5有限字段验证；本轮没有完整原生分支见证。
0表示未匹配到完整语法行，不等于没有进程、没有调试附加、回调未发生或唯一脚本故障。
两例Git128已经可见，但具体根因与失败阶段尚未证明；历史Root保持UNKNOWN。

旧三轮FAIL保持原件，不重跑或覆盖。未新增模型请求、未读取模型凭据、未改变费用账本，
未更换Git/PATH、选择器、十三hook或期限。完整Git产品、Backup v2、消费者Windows、真实编码质量、
独立Beta及商用R1～R6仍开放。下一步先对原脚本与材料失败接缝求证，不重复相同运行代替定位。
