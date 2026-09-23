---
doc_type: validation-evidence
status: current
version: 1
code_revision: ed48e4e35133d60b172c268becd9e009eacb9442
owners:
  - core
modules:
  - agent
  - session
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_attempt.py
  - tests/benchmarks/test_soak_long_session.py
  - tests/benchmarks/test_soak_evidence.py
supersedes: []
---

# 0.9.3d macOS长会话1000 Turn规模诊断事实

## 1. 证据结论与边界

本目录保存一次真实[`AgentRuntime`](../../../src/harnessix/agent/runtime.py)与
[`SQLiteSessionStore`](../../../src/harnessix/session/sqlite.py)上的连续长会话运行。固定无状态
[`SoakProvider`](../../../scripts/soak_provider.py)只替代模型响应，不保留请求正文；运行通过
[`run_long_session`](../../../scripts/soak_long_session.py)创建单一Thread，执行5个预热Turn和1000个正式Turn，
逐Turn持久化并在结束时核对Session投影、Event Replay和Provider请求计数。原始文件由
[`read_published_run`](../../../scripts/soak_evidence.py)和
[`read_attempt`](../../../scripts/soak_attempt.py)从本目录复制件独立重读通过。

**结论仅为规模诊断事实，不是0.9.3d发布PASS，也暂不用于冻结Threshold Profile。** Manifest的`baseline`
状态只证明Run满足当前合同的1000 Turn最低数量和干净Revision检查。现有Runner没有专门断言Context Inspection或
Compaction事件，也未覆盖完整产品stdio/TUI启动、SDK容量、Artifact增长、Action故障和重启场景；即使本Revision的
[CI 35806501607](https://github.com/carrie1988/Harnessix/actions/runs/35806501607)六实例通过，也不能用一次运行替代
独立阈值Profile、第二次同平台复验或Linux/Windows正式负载。

## 2. 固定身份与运行环境

| 项目 | 事实 |
|---|---|
| 源码Revision | `ed48e4e35133d60b172c268becd9e009eacb9442`；运行前工作树干净；对应CI六实例通过 |
| Run ID | `aa4d3e02a93049a88c2239c33e93d8a2` |
| 时间（UTC） | 2026-09-23 01:30:14.574454 至 01:44:52.827956 |
| 平台/Python | macOS / 3.13.8 |
| 硬件档位 | 16逻辑CPU、51,539,607,552字节物理内存，`c16-m48`；未把容器有效上限纳入档位 |
| 测量边界 | `long_session → core_runtime`，不是完整Coding Agent产品端到端启动 |
| 规模 | 单Thread、5预热Turn、1000正式Turn；Provider固定脚本请求1005次，无网络模型调用 |
| 正式样本 | `turn_local` 1000条、`rss_peak` 1条；预热Turn时延保留原始样本但不进入分位数 |
| 故障计数 | 六类固定计数均为0；本次没有故障注入，不证明故障矩阵通过 |

## 3. 可重算数值

单位为纳秒或字节；P50/P95/P99使用`nearest_rank_v1`从原始样本重算，不是性能阈值。

| 指标 | 样本数 | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| `turn_local` | 1000 | 596182250 | 1173980291 | 1265983792 |
| `rss_peak` | 1 | 344817664 | 344817664 | 344817664 |

Session DB端点水位从98,304增至10,649,600字节，WAL前后端点均为0；这些是区间端点值，**不是**运行期间
磁盘峰值。`rss_peak`为Runner进程`getrusage`的峰值RSS，macOS当前运行时原始单位经受控探针判为bytes，按identity
归一化；它不等于单个Turn的内存占用。运行耗时约14分38秒，墙钟差值不参与Turn时延统计。

## 4. 原始文件与提交完整性

| 文件 | SHA-256 | 职责 |
|---|---|---|
| [`samples.jsonl`](aa4d3e02a93049a88c2239c33e93d8a2/samples.jsonl) | `f75614ee921e436db67c8dadc5e5039ec721d284db30024239ca0ea38d339960` | 1005条Turn时延和1条RSS低敏原始样本 |
| [`manifest.json`](aa4d3e02a93049a88c2239c33e93d8a2/manifest.json) | `b1394ca6486e9f8dd4de06ccb5bf486cb419cf1016a5dfc58f7330f12b7a0977` | 运行身份、环境、样本数、分位数、水位和故障计数 |
| [`COMMITTED.json`](aa4d3e02a93049a88c2239c33e93d8a2/COMMITTED.json) | `c33b0a985c0bc4dd26fecad89e8f641ff8501005622d7aeb062eff5baa69319a` | 绑定Manifest原始字节摘要的Run提交标记 |
| [`STARTED.json`](attempts/aa4d3e02a93049a88c2239c33e93d8a2/STARTED.json) | `b94d4f203869eeebe34f95f900969843441c4874a521a7b6ad908b948d94d677` | 负载前的Attempt开始事实 |
| [`FINAL.json`](attempts/aa4d3e02a93049a88c2239c33e93d8a2/FINAL.json) | `19f599c4e1a2773d9f55f585caa2eabf4201275ecf91da4c0997e06f93bd4970` | `committed/publishing`终态，与Manifest摘要一致 |

复制件与生成位置五个文件逐字节相同。本目录的Run子目录仅有上述三个Run文件；Attempt目录仅有两个Attempt文件。
Reader重新核对规范JSON、固定目录集合、Manifest和样本SHA-256、样本数量、统计、RSS，以及Attempt的Run ID、场景、
Revision和Manifest摘要。低敏扫描未发现个人绝对路径、用户名或凭据特征；证据不保存Prompt、工具正文、业务Thread ID
或临时Session数据库。仅有提交标记或仅有成功Attempt都不能独立构成发布PASS。

## 5. 后续验证与不可外推范围

1. 补齐长会话Context/Compaction断言和全局期限；必要时重新运行1000 Turn，不能在本Run中补写未采集指标。
2. 完成另外五类场景，其中多Thread旧诊断证据见[上一版](../soak-macos-2026-09-23-v1/README.md)；旧Run的失败CI属性不因本次成功而改变。
3. 建立独立Threshold Profile，明确基线来源、平台/硬件档位、样本数和工程余量，然后用**新Run**复验。
4. Linux、macOS、Windows分别执行正式场景；缺平台、缺场景、缺Attempt或失败Run均不得判PASS。
