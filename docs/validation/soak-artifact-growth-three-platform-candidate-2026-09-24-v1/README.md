---
doc_type: validation-evidence
status: current
version: 1
code_revision: 65cbcc5caab2ba167763844e974c9cd7c036ed0d
owners:
  - core
modules:
  - artifacts
  - agent
  - session
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_artifact_growth.py
  - tests/benchmarks/test_soak_artifact_proof.py
supersedes: []
---

# 0.9.3d Artifact增长三平台首轮候选性能FAIL诊断

## 1. 结论与证据身份

在[三平台正式基线和封印Profile](../soak-artifact-growth-three-platform-2026-09-24-v1/README.md)提交后，Revision `65cbcc5caab2ba167763844e974c9cd7c036ed0d`的[手动候选工作流 35961898619](https://github.com/carrie1988/Harnessix/actions/runs/35961898619)三个独立Job均完成执行并产生完整证据：**Linux与macOS报告为`FAIL/limit_exceeded`，Windows报告为`PASS/within_limits`**。原始文件及上传件来源见[证据Manifest](bundle-manifest.json)，定量结果见[评审包](review-packet.json)。对应源码的[常规CI 35961875693](https://github.com/carrie1988/Harnessix/actions/runs/35961875693)六实例全部成功。

**该Revision的固定Artifact增长场景候选未通过，本目录仅为FAIL诊断原件**：不删除失败报告、不调高已封印Profile、不把Windows单平台PASS解释为场景通过。新基线与第二独立候选须在修复负载设计后于新Revision重新取得。

## 2. 失败事实

| 平台/档位 | 候选Run ID | 报告状态 | 越限指标 | 候选发布P50/P95/P99（ns） | 基线发布P50/P95/P99（ns） |
|---|---|---|---|---|---|
| Linux `c4-m16` | `3c502835b6c546af847c9358b58badb0` | FAIL | `artifact_publish.p95`、`artifact_publish.p99` | 17,605,008 / 177,564,480 / 198,661,743 | 16,719,721 / 49,069,968 / 50,031,242 |
| macOS `c3-m7` | `cbe080e8f09c4153a21337b2f1374a5c` | FAIL | `artifact_publish.p99`、`artifact_read.p99` | 12,529,125 / 27,424,500 / 74,492,834 | 11,250,250 / 29,062,584 / 30,726,459 |
| Windows `c4-m16` | `1b0bc3dcd6424e0d9c1ff6ed4c71c68c` | PASS | 无 | 84,980,800 / 156,030,600 / 173,691,700 | 82,476,100 / 164,972,300 / 183,639,800 |

macOS候选读取P99为41,531,291 ns，基线为10,578,125 ns（上限21,156,250 ns）。三份候选的取消、超时、EOF、UNKNOWN、重复效果、孤儿均为0，22件Artifact的发布、195次分页与到期清理语义全部成立；FAIL只涉及时延分位数。

## 3. 根因分析

1. 基线与候选运行的是**同一产品代码**（两Revision之间只有文档变更），且三个平台候选的`artifact_publish`与`artifact_read`的**P50均与基线一致**，不存在实现回归。
2. 发布指标是双峰分布：小件约10～20毫秒，近限件（1 MiB正文单事务落盘并fsync）约30～200毫秒。正式样本仅20件，其中近限件5件；`p95`/`p99`落在近限件子样本的最高两名，单件共享Runner磁盘抖动即可移动分位数。
3. 基线运行（`b3e0d6f`）在Linux/macOS恰好取得平静磁盘条件（近限件尾部约30～50毫秒），候选运行（`65cbcc5`）遇到约2～4倍磁盘条件漂移；Windows基线本身即在噪声条件下取得（尾部约180毫秒），因此Windows候选在同档噪声下自然通过。
4. 结论：失败机制是**基线与候选之间的共享Runner I/O条件漂移**，20件正式样本的双峰尾部分位数在100%工程余量下无法区分环境漂移与真实回归；这不是产品代码性能缺陷，也不允许据此调高 sealed Profile。

## 4. 处置与边界

按既定门禁规则，本次FAIL原件保留，修复方向是负载设计而非阈值：正式负载从20件提高到60件（近限件由5件增至15件），使`p95`成为近限件子样本的第3高值而非第2高值，改善尾部估计的代表性；`p99`仍是近限件最高值，残余环境风险在新基线归档时如实记录。阈值方法（时延上浮100%、RSS与文件增长上浮50%）不变，预热2件不变（v3合同约束），旧基线与旧Profile保持只读。新基线、新Profile与第二独立候选见后续归档；本FAIL不改变`action_recovery`已取得的场景PASS。
