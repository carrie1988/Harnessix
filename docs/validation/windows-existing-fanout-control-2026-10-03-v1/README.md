---
doc_type: validation-evidence
status: current
version: 1
code_revision: abcde35e9fe1435c79b9cea20d470c6f4c323d77
owners: [core]
modules: [product_config, delivery, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_material_snapshot_differential.py
  - tests/governance/test_git_material_snapshot_differential.py
  - tests/governance/test_git_minimum_commit_probe.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# 同源fanout／blob及真实Windows共享反例验证

## 1. 结论与覆盖边界

本包落实[完整设计第14节](../../changes/m09-r4-windows-minimum-commit-probe.md#14-既有fanout目录与单对象持有的同源控制)，
新增测试域控制，不修改生产句柄、批准、Owner、材料或费用合同。原Windows
[四格失败结果](../windows-git-snapshot-differential-2026-10-03-v1/README.md)保留，Root UNKNOWN不变。
原生执行尚未取得，不能提前登记生产修复或原SDK通过。

## 2. 源码、流程与不变量

- [真实控制模块](../../../tests/product_config/test_git_material_snapshot_differential.py)：_run_diagnostic复用原真实I/O；
  _same_seed_repositories完整copy2并验证对象内容、目录和独立单链接；test_same_seed_repository_with_single_hold分别持有目录或blob。
- [治理反例](../../../tests/governance/test_git_material_snapshot_differential.py)：实际hardlink复制和对象内容漂移拒绝、
  原操作末端期限、互斥持有、原节点和workflow结构不变。
- [原Windows API](../../../src/harnessix/delivery/git_material_native_windows.py)：访问、共享、路径和inode检查没有变化。
- [既有手动workflow](../../../.github/workflows/windows-git-minimum-commit-probe.yml)：保留原四格及原两SDK，
  新增两个同源配对和三个Win32控制；所有结果沿用步骤元数据，不新增收集器。

一次真实seed的baseline blob与最低SHA256 Commit都位于91目录，但对象文件不同。
两臂复制后、任何写入前先完整比较初始对象字节和目录形状，并验证非同inode、st_nlink=1及目标不存在。
直接臂与单持有臂各有原45秒预算；原20秒命令预算和有界OID读取保持。完整正文通过独立cat-file回读。

创建控制使用真实os.open、O_CREAT／O_EXCL／O_RDWR与0444；链接控制调用真实CreateHardLinkW。
因果反例必须实际证明：无持有链接成功，原share1目录持有产生Win32错误32且无目标，
仅补share3后链接成功；其局部包装不模拟DLL，原0x81访问和完整原路径／inode验证保持。
失败即假设未证明，不据此放宽生产共享。源码前冻结、架构／时序／数据流图、字段、伪代码与异常边界见原设计。

## 3. 验证与交付

本机五件关联测试文件实际520通过、3跳过、零失败／错误；三个跳过项为Windows真实创建、链接和共享反例。
原两个SDK选择器另行实际2通过、零失败／错误／跳过，不外推为Windows或完整产品验收。
初始两件测试文件亦通过；后继520已包含该范围，不重复累加。全部计数来自实际XML。
Ruff、精确变更Secret扫描及自检通过；文档527份、12187链接、1016图结构检查零发现，
第14节三幅Mermaid真实渲染并视觉复核通过。Windows控制在POSIX明确跳过，不计通过。
十八固定输入只更新手动workflow的LF／CRLF长度和摘要；其余17行、所有其他合同字段、原SDK及13接点保持。
完整原件、设计前置记录、图示、Review Packet和精确输入差分保存于私有
windows-existing-fanout-control-20261003-v1目录，目录0700／文件0600。

固定新候选只派发一次attempt1；仅读取Run／Job／step API元数据，不读取Git、CDB或Job业务日志。
本包不关闭Windows消费者、完整Git产品／Backup v2、真实R3、费用未决、独立Beta及最终R1～R6。
