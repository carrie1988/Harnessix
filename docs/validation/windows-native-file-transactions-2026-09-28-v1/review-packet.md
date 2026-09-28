---
doc_type: validation-evidence
status: current
version: 1
code_revision: 87f93533713a7b640b0d41f4c1b781693c6616ee
owners: [core]
modules: [delivery, workspace, product_config, trusted_actions]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/delivery/test_windows_filesystem.py
  - tests/delivery/test_windows_io_contracts.py
supersedes: []
---

# Windows原生文件事务评审包

## 1. 阅读顺序

1. [详设](../../changes/m09-r4-windows-native-file-transactions.md)：需求、目标、不变量与原生API方案。
2. [共享Runtime](../../../src/harnessix/delivery/filesystem.py)：持久publishing、成员事实与只观察恢复。
3. [成员端口](../../../src/harnessix/delivery/windows_filesystem.py)：原Root/父链与before检查，清理边界。
4. [IO](../../../src/harnessix/delivery/windows_io.py)：NT信息类65、UTF-16 ABI、完成状态和共享模式。
5. [元数据](../../../src/harnessix/delivery/windows_metadata.py)：流、属性、链接和Owner/Group/DACL。
6. [原生测试](../../../tests/delivery/test_windows_filesystem.py)：正常写入、漂移、确认丢失与硬退出。
7. [默认产品审批](../../../tests/product_config/test_server_and_cli.py)：模型意图经Review/Approval后才写入。

## 2. 必查问题

- 是否始终使用原Source Root身份，错误Root不能靠相同内容追认？
- 父链是否保持固定，Junction/ADS/特殊权限是否明确拒绝？
- 创建是否不覆盖，替换源是否仅共享Delete而从未共享Write？
- 当前名称是否仍指向原File ID，最后检查不是不合作写者的原子CAS是否明确？
- NtSetInformationFile返回和IO_STATUS_BLOCK是否都确认，PENDING是否失败关闭？
- Rename请求后是否绝不清理句柄，包括确认丢失时？
- 重开是否只观察，未开始成员是否不在取消/租约失效后继续？
- windows-ntfs-v2是否与原生语义绑定，而POSIX摘要和公共合同保持？
- Scripted Provider专项与真实模型质量、原生端口与消费者安装是否明确分开？

## 3. 复验

精确源码、命令、日志和原生CI见[verification.json](verification.json)。
在macOS重复六组受影响回归，在Windows重复59项专项及完整必要模块回归，
最后核对Wheel逐字节来源、Schema/可读性与Manifest。
不得把缺失CI、消费者OS测试、真实模型或独立Beta改记为成功。

## 4. 评审结论范围

新增文件成员端口及默认审批专项有固定版本证据；没有第二状态机、服务、Store或权限绕过。
完整三平台商业支持、R3实际质量、R4安装/Git/恢复及R5真实Beta仍开放。
不把低优先级发行治理引入功能关键路径，也不将其未完成状态改为已通过。
