---
doc_type: validation-evidence
status: historical
version: 2
code_revision: d64054638a03bca008f5b392fd0edf23aa4cd63b
owners:
  - core
modules:
  - agent
  - app_server
  - session
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_many_threads.py
  - tests/benchmarks/test_soak_evidence.py
supersedes: []
---

# 0.9.3d macOS多Thread事实基线（单次）

## 1. 证据定位

本目录保存`harnessix.soak-scenario/v1`中`many_threads → app_service`场景的一次真实产品模块规模运行。
运行在500个持久Thread上，预热一次、正式重启三次；每次均调用当前`AgentRuntime`启动恢复和
`AgentApplicationService.list_threads`完整分页，模型夹具没有请求。证据由
[`run_many_threads`](../../../scripts/soak_many_threads.py)生成，并经
[`read_published_run`](../../../scripts/soak_evidence.py)从仓库内复制件重新校验。

这只是**单平台、单次诊断事实**。`baseline`是Manifest运行状态，不是性能`PASS`。该Revision的
[CI 35804027413](https://github.com/carrie1988/Harnessix/actions/runs/35804027413)在macOS和Windows基准测试Job失败：
超小共用超时预算在Runtime初始化期间到期，导致列表超时断言误分类，并在Windows触发临时数据库清理竞态。
因此本Run**不得用于冻结发行Threshold Profile**；原始数值保留用于定位，不选择性删除失败Revision证据。
独立Threshold Profile、
第二次运行、Linux/Windows正式负载、其余场景及失败运行事实保留均未完成；本证据不能关闭0.9.3d，
更不能证明1.0商用规模。

## 2. 固定身份与负载

| 项目 | 实际值 |
|---|---|
| 源码Revision | `d64054638a03bca008f5b392fd0edf23aa4cd63b`；运行前工作树干净，但该Revision跨平台CI未通过 |
| Run ID | `4bec5614292143d19167a1009d28bdd0` |
| 运行时间（UTC） | 2026-09-23 00:54:16.898487 至 00:54:28.451293 |
| 平台/Python | macOS / 3.13.8 |
| 硬件档位 | 16逻辑CPU、48 GiB物理内存；`c16-m48` |
| 规模 | 500 Thread，列表每页50，1次预热与3次正式重启 |
| 正式样本 | 启动3、列表页30、RSS高水位1 |
| 模型请求 | 0；未使用真实Provider或API Key |
| 测量边界 | App Service，非TUI/stdio/完整产品组合根 |

## 3. 原始统计与水位

单位为纳秒（时延）和字节（内存、文件）。P50/P95/P99按`nearest_rank_v1`从原始样本重算，
不是人工观察或阈值目标。

| 指标 | P50 | P95 | P99 |
|---|---:|---:|---:|
| `app_service_startup` | 427151000 | 440247292 | 440247292 |
| `thread_list_page` | 219079375 | 427911708 | 431517541 |
| `rss_peak` | 57032704 | 57032704 | 57032704 |

Session DB端点水位从98304字节增至634880字节；WAL两端均为0。上述值不是期间最大磁盘占用。
`rss_peak`来自当前进程`getrusage`，在本机经单位探针判定为原始字节并按identity归一化；
它不是500 Thread独占内存。六类故障计数均为0，仅表示本次没有注入或观察到该类故障。

## 4. 原始文件与完整性

| 文件 | SHA-256 | 用途 |
|---|---|---|
| [`samples.jsonl`](runs/4bec5614292143d19167a1009d28bdd0/samples.jsonl) | `bd18f2bcf28c56ec4573a793bfbe65964a65e707af90f03ab41226e519877542` | 预热/正式低敏数值样本 |
| [`manifest.json`](runs/4bec5614292143d19167a1009d28bdd0/manifest.json) | `6c370bc0f556a514a8fc6a2aaa6afb70bea65b73b4fb300a51385de3c0497c00` | 负载、环境、水位及摘要 |
| [`COMMITTED.json`](runs/4bec5614292143d19167a1009d28bdd0/COMMITTED.json) | `308b5aecf68f26e4fcf38c1ae6b58ecffa0d307d6239b3214f7726fe3a2c1e13` | 最后提交标记，引用Manifest原始字节摘要 |

本目录中的Run只包含以上三个文件；严格Reader重算并核对Manifest与样本统计。证据白名单不含
Prompt、响应正文、Workspace路径、账户、主机名、业务Thread ID或凭据。复制件与生成位置的
Manifest摘要相同；原始数值文件未改写。

## 5. 适用边界与待办

1. 这次运行只能作为macOS本机`app_service`边界的诊断事实；由于该Revision跨平台CI失败，不能用于冻结或通过阈值。
2. 正式发布需独立冻结Profile，再在同平台/档位执行新Run验证；三平台均需真实正式负载。
3. 当前Runner异常结束时不会保留低敏失败Manifest，硬退出也不会留下可判定Run；发布门禁前必须补齐。
4. 当前列表实现按页读取全部剩余Thread，可能随规模增加退化；本次500 Thread观测只定位风险，
   不预先放宽完整性或跳过后续压力验证。
