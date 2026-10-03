---
doc_type: validation-evidence
status: current
version: 1
code_revision: cf35055668f9529c9a677e5226a98bbd40c4cefd
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_material_stream_ownership.py
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_snapshot_lifecycle.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# 完整材料快照流FD归属验证

## 结论与边界

原真实`io.open`审计在FD复制后拒绝FileIO构造时，复制FD未登记到资源栈，连续六次失败增长六个FD。
修复后FD由原Resources独占关闭，FileIO以`closefd=False`借用；审计普通异常和控制异常都保留原异常，
每次作用域退出后FD为EBADF，无FD增长或命名快照残留。

正常路径仍为原只读、普通文件、offset0和完整二进制材料；提前关闭流后FD保持由栈持有，
原作用域退出后关闭。没有路径重开、权限扩张、PIPE替代或新的Owner。
这是macOS实测的失败清理修复，发生在Git启动前；**不是Windows Git128根因、Windows SDK或商用验收通过**。

## 分阶段结果

| 阶段 | 实际结果与范围 |
|---|---|
| 原源码初版故障回归 | 2 FAIL / 2 PASS；两种异常各六次，FD各增长六个 |
| 原源码最终借用合同回归 | 4 FAIL；增加正常提前关闭流的明确借用契约，未覆盖初版原件 |
| 新源码最终回归 | 4 PASS，无FAIL/ERROR/SKIP；两种异常、SHA1/SHA256正常二进制流 |
| 原受影响回归 | 18个明确文件，1260 PASS / 4 Windows原生SKIP，无FAIL/ERROR，88.687秒 |
| 来源稳定性 | 原受影响运行468件输入前后零漂移；新unit另列原／新源码与测试完整SHA |

不将重叠验证累加为全仓或Windows成绩。原死亡屏障、完整8MiB三类型两格式、Owner退出、
独立批准回读和有限观察拒绝合同仍由原测试覆盖。本包新增模型请求及CI dispatch为0。

## 源码、设计与固定输入

- [总体与详细设计第13节](../../changes/m09-r4-git-object-material-input.md#13-快照流封装的fd归属与失败清理)：需求、架构、流程／时序／数据流、字段、伪代码、失败、安全与部署。
- [实现](../../../src/harnessix/delivery/git_material_native.py)：只改变原`_snapshot`末端FD归属。
- [真实审计回归](../../../tests/product_config/test_git_material_stream_ownership.py)：独立基础Python子进程，未mock fdopen、dup或snapshot，不启动Git。
- [模块设计](../../modules/delivery.md)：原材料输入与资源关闭归属。
- [验证数据](verification.json)：原／新实现、18输入单行差分、实际结果与未证明项。

Windows名单仍为18件，仅native一行的LF／CRLF四叶身份变化；其余17行、两SDK选择器、
PE/PDB、13hook、20/45/240/300秒期限、原分支／proof／SDK门均保持。
原安装实现摘要消费实际新代码，旧Plan或批准不能按新实现重放。

## 原件与后继

原RED／GREEN、日志／XML、来源锁、独立Review Packet、三张实际渲染图和交付manifest
分别保存在私有`git-snapshot-stream-test-20261003-v1`、`git-snapshot-stream-review-20261003-v1`
及`git-snapshot-stream-ownership-20261003-v1`目录；0700目录／0600文件，不发布正文、路径或凭据。

R1整体、完整Git业务交付／Backup v2、Windows Git128、R3质量及费用未决、消费者Windows、
独立Beta和最终同候选R1～R6继续开放。无新增Windows运行，不修改历史原生失败。


## 独立复核与开发候选

限定独立审查未发现P0／P1／P2；原两种真实审计负例捕获旧FD泄漏，新实现原unit四项通过，
只读stdin继承、退出复查及LIFO关闭次序另有实际观察。独立宿主初次因Apple Git2.24.3
不支持object-format产生四项fixture错误，原日志保留；使用既有支持Git2.53.0后原四项通过。
这些结果不累加，不扩展为全面安全或消费者平台验收。

完整Git开发候选仅在原native字节与基线相等后接入相同实现与unit，另四项通过；
其Windows观察器仍为旧16输入版本，没有继承主仓18输入合同或原生成功声明。
该候选的首次文档同步门失败已保留，补模块设计后复验通过；完整业务能力仍关闭。
