# SWE-bench Verified 25 Case 预注册 Pilot

本目录冻结首个外部基准 Pilot 的公开计划：

- 总体：`SWE-bench/SWE-bench_Verified` test，500 Case；
- 选样：`repo_round_robin_sha256_v1`，25 Case，覆盖12个仓库；
- 语义：pass@1、Agent禁网、无模型自动重试；
- 执行实现：`b43509750c579275a1b8cc808776d010a82ed11b`；
- 计划摘要：`bb481e7288ab8b4a9f6e16661094869651fecbc8c2ea900afcf0e5a17245224c`。

该计划不包含`patch`、`test_patch`、测试ID、Hint或`eval.sh`。真实 Agent 执行与官方评分必须使用上述
实现提交和计划摘要；评分完成前不得声明外部基准结果。总体设计见
[`docs/changes/swebench-adapter-verified-25-pilot.md`](../../../docs/changes/swebench-adapter-verified-25-pilot.md)。
