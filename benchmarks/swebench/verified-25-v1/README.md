# SWE-bench Verified 25 Case 预注册 Pilot

本目录冻结首个外部基准 Pilot 的公开计划：

- 总体：`SWE-bench/SWE-bench_Verified` test，500 Case；
- 选样：`repo_round_robin_sha256_v1`，25 Case，覆盖12个仓库；
- 语义：pass@1、Agent禁网、无模型自动重试；
- 计划摘要：`ae58dc6b4df6f31738b388baf7fdb15b73044ca83c4f3509687b8be5cb21e073`。

该计划不包含`patch`、`test_patch`、测试ID、Hint或`eval.sh`。当前仅完成预注册与适配器验证，
尚无25 Case真实模型执行或官方评分结果。总体设计见
[`docs/changes/swebench-adapter-verified-25-pilot.md`](../../../docs/changes/swebench-adapter-verified-25-pilot.md)。
