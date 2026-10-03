---
doc_type: validation-evidence
status: current
version: 1
code_revision: 92bdb4d0362cf24dc1907dc3a50fdae5700d176f
owners: [core]
modules: [workspace, delivery, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_store.py
  - tests/delivery/test_planner.py
  - tests/workspace/test_snapshot_capacity.py
  - tests/product_config/test_product_state_backup.py
supersedes: []
---

# Workspace 完整记录长路径写读不对称真实复现报告

## 1. 固定来源、目标与结论

本报告将[完整父闭包设计候选](../../changes/m09-r4-workspace-parent-closure.md)中的
记录长度静态风险转化为实际文件系统、原 Planner 和原 Store 的运行证据。
本地文档候选为 `92bdb4d`；六件关联生产源码与已发布 `007d2bd` 原字节相等。
环境为 Darwin arm64、Python 3.12.7。仅操作独立临时夹具，不读取用户状态或工作区。

**原 `save()` 可以成功返回 `prepared`，但同代正式 Reader 随后拒绝该记录。**
短、长路径两组均为 200 个真实文件、209 个原观察 Resource、2600 字节 before／after 镜像，
没有触顶原资源、mutation、单文件或总镜像容量。变化项为八层共享父链的组件长度。

| 项目 | 短路径正对照 | 长路径失败 |
|---|---:|---:|
| 每层组件 UTF-8 字节 | 12 | 180 |
| 最长目标相对路径 UTF-8 字节 | 112 | 1456 |
| mutation／Resource | 200／209 | 200／209 |
| 完整记录 UTF-8 字节 | 147549 | 691196 |
| `save()` | 返回 `prepared` | 返回 `prepared` |
| 同一 Store `load()` | 与返回记录全等 | `delivery_store_corrupt` |
| 关闭后只读重开 | 与返回记录全等 | `delivery_store_corrupt` |
| Store 操作后原 Snapshot 再验证 | 通过 | 通过 |

本缺陷已复现，尚未修复。没有执行 Workspace 发布或用户文件写入，不能将源目录保持不变
解释为 Store 写读合同已正确。

## 2. 正式源码与实际执行链

1. 通过原生 `dir_fd` 和 `O_NOFOLLOW` 创建真实父目录及 0644 普通文件，避免长绝对路径的宿主 API 限制干扰。
2. 原 [`prepare_workspace_transaction`](../../../src/harnessix/delivery/planner.py) 对全部文件及父目录捕获原 Snapshot，读取真实镜像并完成来源复核。
3. 原 [`SQLiteWorkspaceTransactionStore.save`](../../../src/harnessix/delivery/store.py) 写原 CAS、完整记录及初始事件，成功返回。
4. SQLite 查询真实 payload 的 UTF-8 字节数和事件数量，不伪造数据库行或修改序列化器。
5. 原 Snapshot 在 Store 操作后再次完整验证，证明未发布 Workspace 内容。
6. 同一 Store 读取及关闭后原正式只读 Reader 重开分别核验；长路径两次均拒绝。

未使用 `model_construct`、替代 Snapshot、修改常数、拆事务、monkeypatch 或畸形记录制造失败。
原目录观察、无跟随句柄、完整 Plan 指纹和 CAS 机制均参与实际链路。

## 3. 失败原因与持久化语义

`save` 和状态推进将完整 Record JSON 同时写入当前行及每个事件。
`_decode` 检查 `len(payload.encode()) <= 512*1024`，但写入路径没有对应准入或大记录引用编码。
长路径在资源与 mutations 中重复出现，使合法正式规划结果超过 Reader 上限。

本次长路径数据库实际保存一条当前记录及一条初始事件，二者均为 691196 字节；
不是缺 Blob、变化中的来源、错误请求身份或测试替代序列化造成的拒绝。
旧数据不删除、不修改、不重签，原失败证据保留。

## 4. 可执行回归与验证范围

私有交付包保存两例完整可执行回归、原日志、JUnit XML 和命令退出码：
短路径要求保存后重开记录全等；长路径提出相同要求，不把原损坏拒绝改成成功断言。
实际 **2 项、1 通过、1 失败、0 错误、0 跳过，pytest 退出码 1**。
失败例为 `test_long_path_saved_record_reopens`，不设置 xfail、不改变原正式选择器或阈值。

这是隔离原实现的失败复现，不是全仓回归、三平台门禁、完整产品审批调用或修复验收。
新增验证没有模型／网络请求，也没有改变费用规则、用户文件或产品源码。
夹具、日志及数据库留在私有路径；公开包只提供有限事实与摘要，不复制数据库或原长路径内容。

## 5. 后继与商用边界

后继需按完整闭包及引用记录设计同时闭合规划、保存、事件重读、批准、来源认证和备份。
仅提高 Reader 常数，或在写入前额外拒绝原合法业务输入，不能作为完整容量整改完成。
该原失败要求在同样真实输入的新实现下保存及重开成功，并保持原全部安全与资源边界。

完整 Git 产品、Backup v2、R3 真实质量、消费者平台、独立 Beta 及同候选 R1～R6 仍开放。
本报告不把失败成功复现称为产品功能通过。

## 6. 验证包索引

- [result.json](result.json)：两组实际完整链、正式来源和原字节摘要。
- [regression-result.json](regression-result.json)：JUnit 实际计数及原失败例，不含原 traceback。
- [SHA256SUMS](SHA256SUMS)：两份有限结构化资料摘要。
