---
doc_type: adr
status: draft
version: 1
code_revision: ae560ad26899ae8ecb55bfc5c0ff2539727f5088
owners: [core]
modules: [product_config, session, secrets]
related_adrs:
  - docs/adr/0104-managed-session-key-and-default-root.md
related_tests:
  - tests/product_config/test_session_key.py
  - tests/product_config/test_session_key_dpapi.py
  - tests/product_config/test_session_key_windows.py
  - tests/product_config/test_managed_session_root.py
supersedes: []
---

# ADR-0104：默认产品独立本机持久Session密钥

## 背景与决策

库级认证需要稳定独立Key，不能默认继续信任未知旧历史或按Scope重新签发。
默认Product Server强制本机托管Binding；先Key与Session验真，再构造Provider和开放Protocol。
选定POSIX规范私有文件/FD与Darwin扩展ACL拒绝；Windows当前用户DPAPI加原生Owner/protected DACL与句柄链。

## 选型、约束与取舍

不复用模型Credential，不依赖一次性Epoch，不采用DPAPI机器作用域、明文失败回退或随机重启Key。
独立本机文件避免引入交互Keyring与额外包，但POSIX不是不可导出或加密后端，不防同UID任意代码。
原库无Key失败关闭，完整原候选在锁内恢复；不补签历史、不覆盖有效Key、不删除未知原数据。
5秒准入限制与线程结算分离，不声称硬抢占OS调用。原Event/Schema/公共DTO不变，Windows实际验收独立记录。

## 验证与剩余边界

[总体详设](../changes/m09-4a-managed-session-key-and-root.md)包含Native API、字段、流程、取消与部署边界。
[研究](../research/managed-session-key-and-root.md)区分固定开源快照与主源文档；未复制泄密实现。
测试包括真实POSIX/ACL/进程退出、默认Root/SDK/双重启及失败清理；Windows替身不能替代原生运行。
Key备份/维护CLI、Artifact正文跨Epoch、物理DB归属、全部Provider/Owner/远端MCP、来源权利和Beta仍开放。
本ADR为范围评审草案，不代表0.9.4a或发布关闭。
