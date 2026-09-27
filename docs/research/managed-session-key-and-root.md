---
doc_type: source-research
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

# 默认产品持久密钥与原启动边界源码研究

## 1. 固定本地开源源码

| 来源 | 固定Revision/文件 | 求证结论与取舍 |
|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`，`codex-rs/keyring-store/src/lib.rs`与`codex-rs/login/src/auth/storage.rs` | 存储端口区分缺项、失败与更新；本项目不得把缺Key解释为可重建旧事实 |
| Codex Windows Sandbox | 同上，`codex-rs/windows-sandbox-rs/src/dpapi.rs` | 机器作用域服务于其提升/非提升场景；本项目当前用户Key不借用该作用域 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`，`packages/opencode/src/auth/index.ts` | Auth以600私有文件保存；读取失败到空配置的便利语义不能用于认证历史补签 |

仅参考端口与失败思路，不复制实现或扩大来源权利结论；不使用泄密源码实现本密钥后端。

## 2. 主源平台合同

- [Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)：当前用户、非交互、OS输出释放；机器作用域与本场景不符。
- [CryptUnprotectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata)：原用途熵匹配与完整性失败，不能明文回退。
- [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[GetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo)：原句柄、READ_CONTROL、Owner/DACL与缓冲所有权。
- [SDDL](https://learn.microsoft.com/en-us/windows/win32/api/sddl/nf-sddl-convertstringsecuritydescriptortosecuritydescriptorw)、[Token](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation)：当前用户SID与新对象明确protected DACL。
- [MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)：不覆盖、不跨卷复制；未声明目录fsync或断电验收。
- [Python OS](https://docs.python.org/3/library/os.html#os.open)：dir_fd、no-follow、描述符与有限文件IO。
- [Apple ACL](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/acl_get.3.html)：FD原扩展ACL；本机SDK核对`ACL_TYPE_EXTENDED=0x100`和实际Native缺ACL/有ACL行为，不仅依赖mode bits。

## 3. 当前Harnessix源码与观察

原[Product Server](../../src/harnessix/product_config/server.py)在构造Bundle后创建无Binding的Session；
旧库在当前材料未命中时可被消费，但没有原保护事实，普通SHA不足以证明来源。
[认证Session](../../src/harnessix/session/sqlite_publication.py)已提供原新CAS同事务与Checkpoint，因此复用Binding而不是新Store。

本实现补独立托管Key、规范文件/ACL/Owner、不可覆盖发布和唯一线程结算；Root旧历史先拒绝，不从模型凭据派生Key。
真实Root与SDK测试保留Provider工厂/传输驱动替身；实际Key、Store、Scope与原历史不使用证明替身。
六类Windows原生测试本机跳过，不声明Windows通过。新观察与固定实现/发行物证据后续独立冻结，原证据不回写。

默认产品的实际审批与SDK重放并发暴露了独立`events`入口缺少读事务的问题。
沿[Session读入口](../../src/harnessix/session/sqlite.py)与
[前缀认证](../../src/harnessix/session/sqlite_publication.py)确认多SELECT跨版本；
两个真实连接的确定性测试复现后，仅在独立事件读取入口建立一致快照，不放宽原证明或增加重试。

## 4. 不变条件与剩余研究

Key不存在但原DB/WAL/SHM存在不得生成替代；新Key先于库头；恢复复用原候选/原Key。
原Scope安全事实不等于当前公开许可；Codec、Schema和原字节保持。
Key保护恢复包、维护CLI、Artifact持久正文证明、实际三平台部署、Owner/SDK/MCP及来源权利仍需完成。
[完整设计](../changes/m09-4a-managed-session-key-and-root.md)与[ADR](../adr/0104-managed-session-key-and-default-root.md)定义当前正式范围。
