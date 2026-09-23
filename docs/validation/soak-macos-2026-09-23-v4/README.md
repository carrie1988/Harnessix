---
doc_type: validation-evidence
status: historical
version: 3
code_revision: d947a57aec68a5f9770a18d1996f58be0237e60d
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
  - tests/benchmarks/test_soak_attempt.py
supersedes: []
---

# 0.9.3d macOS多Thread正式负载诊断

## 1. 证据结论与边界

在干净源码Revision `d947a57aec68a5f9770a18d1996f58be0237e60d`上，
[`run_many_threads`](../../../scripts/soak_many_threads.py)创建500个持久Thread，预热一次后正式重启
[`AgentRuntime`](../../../src/harnessix/agent/runtime.py)三次，每次经
[`AgentApplicationService.list_threads`](../../../src/harnessix/app_server/service.py)完整读取全部Thread。
Run与Attempt均已由磁盘Reader重读，并由独立标准库程序重算摘要链、45条样本的连续序号和正式分位数。

这只是macOS单平台、单次`app_service`边界的规模事实，不测stdio、TUI或完整产品组合根。
Manifest中的`baseline`表示规模、干净Revision和RSS单位检查通过，**不是性能PASS**。尚无事先冻结的
Threshold Profile、冻结后的独立复验或Linux/Windows正式运行；不能用本次数字反推发布阈值。
对应[CI 35813364852](https://github.com/carrie1988/Harnessix/actions/runs/35813364852)的**首次尝试**中，
Windows `windows-trusted-execution` Job有两项Product UI交互测试在提交Prompt后等待Turn状态时超时；
同一Revision的**第二次尝试**重跑该Job成功，最终六个Job均为成功。另一个仅增补本证据文档的Revision
`1bdbbeb`的Windows Job也通过。上述对照说明失败未稳定复现，但未确定原因或排除真实竞态。
本Run仍按诊断证据保存，**在故障原因与稳定性门禁闭环前不得用于冻结发行Threshold Profile**。
首次失败不能因重跑成功而从谱系中抹去；该Job的Benchmarks子集在首次尝试也已通过。
早期[500 Thread诊断](../soak-macos-2026-09-23-v1/README.md)对应Revision的跨平台CI失败，
其原始失败谱系不被本目录覆盖。

## 2. 固定身份与负载

| 项目 | 原始事实 |
|---|---|
| 源码Revision | `d947a57aec68a5f9770a18d1996f58be0237e60d`；运行前工作树干净；Windows CI首次尝试失败、第二次尝试成功 |
| Run ID | `a83f5001531048968b4428a9c660f854` |
| 运行时间（UTC） | 2026-09-23 03:14:09.029937 至 03:14:20.777155；约11.75秒 |
| 场景与边界 | `many_threads → app_service`，`harnessix.soak-scenario/v1` |
| 平台 | macOS，Python 3.13.8，16逻辑CPU、48 GiB物理内存，`c16-m48` |
| 规模 | 500个Thread；每页50个；1次预热、3次正式重启；每轮覆盖全部Thread |
| 样本 | 预热11条；正式启动3条、分页30条、RSS峰值1条；全文件45条 |
| Provider | 确定性夹具，模型请求0；不使用网络模型或API Key |
| 故障 | 六类计数均为0；本场景未注入故障，不代表故障矩阵通过 |

## 3. 可重算统计与水位

分位数从正式样本按`nearest_rank_v1`重算，时延单位为纳秒，RSS单位为字节。

| 指标 | 样本数 | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| `app_service_startup` | 3 | 426932709 | 435170625 | 435170625 |
| `thread_list_page` | 30 | 245870541 | 431874209 | 460647791 |
| `rss_peak` | 1 | 57540608 | 57540608 | 57540608 |

Session DB从98304字节增至634880字节；WAL两端均为0。这是端点水位，不是期间磁盘峰值。
`rss_peak`是整个Runner进程的`getrusage`峰值，并非500个Thread的独占内存；macOS原始单位经探针
确认为字节，按`identity`归一化。与旧诊断数字相近不构成性能PASS或稳定性统计结论。

## 4. 原始文件与完整性

| 文件 | SHA-256 | 作用 |
|---|---|---|
| [`samples.jsonl`](a83f5001531048968b4428a9c660f854/samples.jsonl) | `b0cb0b3c524b76decae7572d79271509b5ebdabc7a920fb6f2a31b8ed55613e1` | 预热/正式低敏数值样本 |
| [`manifest.json`](a83f5001531048968b4428a9c660f854/manifest.json) | `9acc89658001162bee3ed64518e90cedb3b561ac85eebebc48b1289e0540711e` | 场景、环境、样本统计与摘要 |
| [`COMMITTED.json`](a83f5001531048968b4428a9c660f854/COMMITTED.json) | `e0c0c70cb8bcc5cca3d005747196466ba2aa6597cab33352bb11a254c80d2913` | 最后提交标记，绑定Manifest原字节 |
| [`STARTED.json`](attempts/a83f5001531048968b4428a9c660f854/STARTED.json) | `02260505a20f0ddead0a4a3b6fe60b160c06b1ffcf676c4ca729ec08f115e1d6` | 负载前Attempt事实 |
| [`FINAL.json`](attempts/a83f5001531048968b4428a9c660f854/FINAL.json) | `890d7eafdacdb4cde4a61e033097a71d37f232c1f89a60d88863c0301aa62343` | `committed/publishing`终态 |

五个复制文件与生成目录逐字节相同。Run目录严格只有三个文件，Attempt目录严格只有两个文件。
独立复核验证了文件SHA-256、提交标记与Attempt终态、场景身份、样本连续序号和各指标分位数。
隐私扫描未检出本机绝对路径、Session数据库名、Prompt、业务Thread ID或凭据特征；文件只含低敏身份、
环境档位、数值和摘要，不能由此重建业务Session内容。

## 5. 复验与剩余门禁

在仓库根目录执行以下命令可离线复核复制件，不访问模型或用户Workspace：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

run = Path('docs/validation/soak-macos-2026-09-23-v4/a83f5001531048968b4428a9c660f854')
manifest, digest = read_published_run(run)
_, final = read_attempt(run.parent / 'attempts' / run.name)
assert manifest.load.thread_count == 500
assert manifest.sample_counts['app_service_startup'] == 3
assert manifest.sample_counts['thread_list_page'] == 30
assert final is not None and final.outcome == 'committed' and final.manifest_sha256 == digest
PY
```

本Revision的Windows Product UI偶发超时须先定位并证明稳定性，再从干净新Revision重新采集。
后续测试Revision `5ab32753aace369387875a75b50802beb3327d98`仅为这两项测试补充不含Prompt和路径的
超时状态快照，未改变本Run的源码或原始证据，也不构成首次失败的根因修复。
正式发布仍需事先冻结独立Threshold Profile，在**新Run**中复验，并在Linux和Windows运行对应正式负载。
SDK容量、Artifact增长、Action故障和完整产品重启场景尚未完成；本目录不能独立关闭0.9.3d。
