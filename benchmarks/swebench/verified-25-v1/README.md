# SWE-bench Verified 25 Case 预注册 Pilot

本目录冻结首个外部基准 Pilot 的公开计划：

- 总体：`SWE-bench/SWE-bench_Verified` test，500 Case；
- 选样：`repo_round_robin_sha256_v1`，25 Case，覆盖12个仓库；
- 语义：pass@1、Agent禁网、无模型自动重试；
- 执行实现：`ff3d25c83f445430ff3c87f258b8cd5122ea7855`；
- 计划摘要：`64c385da3fcfa350779817c34f5d9088ef1c257ab5c0472d14f174ee1fdd6079`。

该计划不包含`patch`、`test_patch`、测试ID、Hint或`eval.sh`。真实 Agent 执行与官方评分使用了上述
实现提交和计划摘要。总体设计见
[`docs/changes/swebench-adapter-verified-25-pilot.md`](../../../docs/changes/swebench-adapter-verified-25-pilot.md)。

## 已冻结结果

- 25/25 Agent Trial 已完成，输入 `1,349,205` Token、输出 `31,464` Token，本轮已知费用
  `17.323164 CNY`，无本轮未结算请求；
- 生成 3 个非空补丁、22 个空补丁；官方评分仅执行非空补丁，但分母仍为预注册的25 Case；
- 官方固定 Task Repo Dockerfile 本地构建3个镜像，未回退拉取；评分结果为
  `resolved 1/25`、`unresolved 2/25`、`empty_patch 22/25`、基础设施失败0；
- 唯一解决项为`pytest-dev__pytest-7205`；另外两个非空补丁未能应用；
- 独立`verify-results`校验通过。结构化证据见
  [`runs/harnessix-64c385da3fcf-290a0c97ab1a.json`](runs/harnessix-64c385da3fcf-290a0c97ab1a.json)。

该结果是25 Case预注册 Pilot，不是500 Case完整榜单，也不是Harnessix 1.0发布通过结论。

前一执行计划因单次 Provider 传输失败产生未结算费用而在 7/25 停止；原始失败边界保留在
[`runs/partial-bb481e72.json`](runs/partial-bb481e72.json)。当前实现不会重试该计划，而是在每个未结算
请求上按最大请求/响应边界预留费用，并在下一 Case 前执行 80 元总停止线检查。
