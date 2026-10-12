# SWE-bench Verified 25 Case 预注册 Pilot

本目录冻结首个外部基准 Pilot 的公开计划：

- 总体：`SWE-bench/SWE-bench_Verified` test，500 Case；
- 选样：`repo_round_robin_sha256_v1`，25 Case，覆盖12个仓库；
- 语义：pass@1、Agent禁网、无模型自动重试；
- 执行实现：`ff3d25c83f445430ff3c87f258b8cd5122ea7855`；
- 计划摘要：`64c385da3fcfa350779817c34f5d9088ef1c257ab5c0472d14f174ee1fdd6079`。

该计划不包含`patch`、`test_patch`、测试ID、Hint或`eval.sh`。真实 Agent 执行与官方评分必须使用上述
实现提交和计划摘要；评分完成前不得声明外部基准结果。总体设计见
[`docs/changes/swebench-adapter-verified-25-pilot.md`](../../../docs/changes/swebench-adapter-verified-25-pilot.md)。

前一执行计划因单次 Provider 传输失败产生未结算费用而在 7/25 停止；原始失败边界保留在
[`runs/partial-bb481e72.json`](runs/partial-bb481e72.json)。当前实现不会重试该计划，而是在每个未结算
请求上按最大请求/响应边界预留费用，并在下一 Case 前执行 80 元总停止线检查。
