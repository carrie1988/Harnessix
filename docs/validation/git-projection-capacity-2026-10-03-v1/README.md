---
doc_type: validation-evidence
status: current
version: 1
code_revision: 0b1e16ab8482ec324e35f81532ec58a1d09b1b6b
owners: [core]
modules: [workspace, delivery, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_projection_capacity.py
  - tests/workspace/test_snapshot_capacity.py
  - tests/workspace/test_snapshot.py
  - tests/delivery/test_planner.py
supersedes: []
---

# Git 投影资源容量与隐式 cwd 限额错误验证

## 1. 结论与范围

**原父目录扩张容量缺口已复现；隐式 cwd 超限的正式错误准入已修复；完整 Git 产品容量尚未闭合。**
固定基线为 `0b1e16a`，实际新增和修订源码身份以 [verification.json](verification.json) 为准。
原 Snapshot 256 项、单文件 8 MiB、总正文及镜像 32 MiB、mutation 256 项上限均未提高。
正式需求、源码接口、资源字段、流程/时序、失败语义和兼容取舍见
[总体与详细设计第 11 节](../../changes/m09-r4-git-projection-ordering.md#11-完整资源容量核验与-snapshot-错误准入)。

## 2. 原容量实测

使用真实普通微小文件、原 capture/verify 和原 Planner；没有替换 Snapshot、限额或规划器。

| 文件布局 | 叶 read Snapshot | 完整 Planner |
|---|---:|---|
| 127 个独立单层父目录 | 128 项，通过并复核 | 255 项，127 mutations，返回 |
| 128 个独立单层父目录 | 129 项，通过并复核 | 257 项，`workspace_snapshot_limit` |
| cwd 内 255 叶 | 256 项，返回 | 256 项，255 mutations，返回 |
| cwd 内 256 叶 | 原隐式 cwd 后 `ValidationError` | 257 项，`workspace_snapshot_limit` |

128 叶的 before/after 总镜像仅 1664 字节；不是正文、镜像或 mutation 额度触顶。
开发候选新增 8 项通过，第二次运行附带 4 项既有回归共 12 项通过；不能累计为 20 个不同案例。
主仓原代码另执行同一 12 项，12 通过、0 失败/错误/跳过，0.823 秒。
这些结果仅证明原数据表示和原规划器的边界，不证明完整产品 Source、批准、父认领、Lease、T 持久化或投影执行成功。

## 3. 错误缺陷与修复边界

原入口只检查显式资源数；256 叶加隐式 cwd/read 后构造 257 项合同，抛出
`ValidationError / resources / too_long / max_length=256 / actual_length=257`。
新增 10 项测试在修复前为 4 通过、6 失败，0.952 秒；原日志和源码未覆盖。

当前入口补 cwd 后再检查完整数量，逐叶观察前返回原 `KernelError / workspace_snapshot_limit`。
合法 255 叶加 cwd 仍得到原 256 项字段和摘要，显隐请求逐字段相等，原 verify 通过。
access、location 和路径分别参与比较；不能用 write/execute、外部根或另一个目录抵扣 cwd/read。
合格数量仍拒绝重复，超限请求拒绝优先于后续成员观察。

仅 POSIX 观察顺序测试包裹方法以记录路径，真实原方法仍执行；实际只观察 cwd。
该用例不能提升为 Windows 原生事实。没有生产端口替身、用户文件写入、CAS/Store 提交或状态迁移。
修复没有解决 128 个独立父目录下完整 T 的资源扩张，后继必须单独完成容量兼容设计。

## 4. 受影响回归与环境

范围为 `tests/workspace`、`tests/delivery`、`tests/execution`、`tests/trusted_actions`。
命令、执行结果、跳过范围及输入前后摘要记录于 verification.json；不存在完整商用门禁通过声明。
首次执行使用 Apple Git 2.24.3，6 项 Git 原生差分用例失败，原日志保留。
仅改执行环境为已有 Git 2.53.0 后复跑同一完整集合，没有修改生产代码、旧断言、选择项或限额。

```bash
umask 022
git --version  # 此验证使用 Git 2.53.0
PYTHONPATH=src:. PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  -p no:cacheprovider -q \
  tests/workspace tests/delivery tests/execution tests/trusted_actions
```

私有原件保存于 `~/Library/Application Support/Harnessix/verification/git-projection-capacity-integration-20261003-v1/`。
原始容量实验保持在独立只读目录，不被当前修复结果覆盖；公开资料不包含临时根、业务正文或凭据。
文档三幅变化图已实际渲染并检查可读性。静态和文档检查结果以 verification.json 为准。

## 5. 未证明事项

- 全范围产品 Source、认证审批及资源扩张闭合；
- 同一父认领下 T/CAS/Store 耐久保存、Bridge、D、Checkpoint 与独立批准 Commit；
- 联合 Backup v2 和新根恢复；
- 当前候选 Windows 原生行为、三平台发行/升级及消费者安装；
- R3 真实编码质量、独立 Beta 和商用 R1～R6。

本专项不开启默认 Git 能力、不改变 Bridge published 前提、不读取凭据或发起模型请求，不把局部回归提升为发布验收。
