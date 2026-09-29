---
doc_type: validation-evidence
status: historical
version: 1
code_revision: 9b0d1230e702aa72b0ea946e1d49eb19a28d2784
owners:
  - core
modules:
  - session
  - product_config
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_run_restart_soak_candidate.py
  - tests/benchmarks/test_soak_threshold.py
supersedes: []
---

# 认证重启第一轮独立候选与macOS环境不匹配报告

## 1. 结论与固定输入

**三平台固定场景NO_GO**：Linux、Windows为PASS，macOS为`unverified / environment_mismatch`。
[实际Run 36507049020](https://github.com/carrie1988/Harnessix/actions/runs/36507049020)使用源码
`9b0d1230e702aa72b0ea946e1d49eb19a28d2784`，三平台均完成新Run、Attempt与封印Report。
[认证基线与三份Profile](../authenticated-restart-three-platform-2026-09-29-v1/README.md)已在候选前冻结。
原未认证Profile、旧容量FAIL及本次不可比较结果均保持原判定，不重新冻结阈值。

## 2. 总体流程与正式合同

复用[认证重启详设](../../changes/m09-r1-authenticated-restart-baseline.md)及
[原架构/时序/数据流](../authenticated-restart-three-platform-2026-09-29-v1/README.md#3-总体架构与执行时序)，
不复制采集器或阈值实现。每平台独立500 Thread、一次预热、一次硬退出、三次正式新进程启动。
STARTED v2预绑定原Profile ID/SHA，最终Manifest同值；候选启动时间晚于Profile冻结时间。
业务库、原Key和Workspace不上传，Provider请求及质量Trial计数均0。

## 3. 原报告、资源事实与错误分类

| 平台 | 基线资源档位 | 候选资源档位 | DB增长字节 / 上限 | 原报告 |
|---|---|---|---|---|
| Linux | c4-m16 | c4-m16 | 2,015,232 / 3,022,848 | PASS / within_limits |
| macOS | c3-m7 | c5-m14 | 2,113,536 / 3,182,592 | unverified / environment_mismatch |
| Windows | c4-m16 | c4-m16 | 2,121,728 / 3,182,592 | PASS / within_limits |

macOS启动P95为2,844,207,375ns、RSS为120,487,936字节；不能因为指标较小就跨资源档位接受PASS。
原[Mac Job日志](logs/candidate-macos-job.log)与基线日志均显示`macos-26-arm64`、镜像版本`20260907.0351.1`，
因此不是已证明的OS镜像版本变化；已证明的是相同镜像下CPU/物理内存档位不同。
外部调度为什么分配不同档位尚未排他证明，不将标准标签解释为硬件稳定性保证。

## 4. 独立复核、源码与持久化

[verification.json](verification.json)逐平台重读完整Run/Attempt/Profile/Report，
使用原`verify_and_publish`在私有临时根重算；除新报告ID和核验时间外与原报告全部字段一致。
三个原候选均预绑定正确Profile，源Revision及Manifest SHA与FINAL一致。
原件位于`raw/<platform>/harnessix-restart-candidate-evidence`和
`raw/<platform>/harnessix-restart-candidate-reports`，完整集由[Manifest](manifest.json)验真。

| 源码 | 核验职责 |
|---|---|
| [run_restart_soak_candidate.py](../../../scripts/run_restart_soak_candidate.py) | 选择认证集合、来源/余量及数学阈值前置校验、预绑定和原报告重读。 |
| [soak_threshold.py](../../../scripts/soak_threshold.py) | 环境不匹配先于性能判定；不能用更强硬件的成绩跨档位PASS。 |
| [soak_environment.py](../../../scripts/soak_environment.py) | 实际CPU数及物理内存读取，不按GitHub标签臆造硬件。 |

## 5. 后继整改与验证边界

后继候选在真实负载前匹配Profile资源档位和Python范围；不匹配时以固定错误拒绝，不创建正式Attempt。
macOS工作流使用版本化标准标签`macos-26`，减少浮动标签影响；资源仍由实际读数严格核验。
[GitHub官方Runner说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
支持该标准标签，但不能替代本次实际容量证据。
后继必须是新Run且仍使用原Profile，不覆写本次结果；若无法获得匹配环境，场景保持未验收。

## 6. 发布与评审

[Review Packet](review-packet.json)保留失败原因、已通过范围和剩余门禁。
本次不关闭认证三平台场景、R1整体、消费者Windows 11、真实20 Trial、用户Beta或1.0。
生产`src`、原采集器及阈值公式与认证基线保持相同；无新付费模型请求。
