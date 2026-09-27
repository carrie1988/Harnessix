---
doc_type: validation-evidence
status: draft
version: 1
code_revision: cef1b17cf63a5bed7d7740d5cbea5bc67728deb2
owners: [core]
modules: [product_config, session, secrets]
related_adrs:
  - docs/adr/0104-managed-session-key-and-default-root.md
related_tests:
  - tests/product_config/test_session_key.py
  - tests/product_config/test_session_key_dpapi.py
  - tests/product_config/test_session_key_windows.py
  - tests/product_config/test_managed_session_root.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 默认产品持久Session密钥与认证Root验证报告

## 1. 当前范围与结论

默认Coding Agent正式装配独立持久Key和强制认证Session，不从Provider凭据派生Key。
macOS实际文件、FD、ACL、锁、进程退出/恢复、Root及候选Wheel真实CLI/SDK双进程已验证。
Windows用户DPAPI与Owner/DACL实现及6项原生测试入口已提供，本机跳过，不声明Windows通过。
密钥备份/迁移、维护CLI、Artifact正文来源证明、整体0.9与正式发行未完成；12件Archive权利仍阻断。

## 2. 六文件证据与设计

[合同事实](contract-facts.json)、[验证](verification.json)、[Manifest](bundle-manifest.json)、
[Review Packet](review-packet.json)与[CI状态](ci-observation.json)固定实现、输入、候选包及边界。
[总体与详细设计](../../changes/m09-4a-managed-session-key-and-root.md)说明需求、架构、4幅新图、
Codec字段、类/接口、原持久流程、伪代码、异常、取消、恢复、权限、安全与部署。
另更新并实际渲染1幅库级边界图。5幅均逐图检查，不只验证语法。
[源码研究](../../research/managed-session-key-and-root.md)与[ADR](../../adr/0104-managed-session-key-and-default-root.md)
记录独立随机密钥、平台保护、默认Root强制绑定及不追认旧历史的取舍。

## 3. 已执行实际验证

- 49项新Key/Root合同：macOS43通过、6项Windows原生跳过。包含6项便携DPAPI ABI替身；替身不作为Windows原生验收。
- 实际macOS三类扩展ACL篡改分别覆盖Root、私有目录、Key；拒绝危险原对象，不自动修改既有ACL。
- 稳定独立身份、候选与正式发布实际OS退出恢复、锁占用、损坏、硬链接、符号链接、父取消、
  准入超时唯一线程结算、失效Scope、自有材料清零与缺Key不替换原身份。
- 真实产品Root双重启和两个离线Scripted Turn证明原事实保留及新写同事务证明；Factory/stdio Driver是替身，Key/Store/Runtime真实。
- 相关1448通过/6跳过；与专项及完整回归重叠，不相加。
- 并发读旧实现两项确定性失败；仅在独立events入口增加读事务后，两项及原审批场景通过，共9项。
  不减少篡改断言、不增加自动重试、不放宽Scope或Artifact Epoch。
- 固定Git归档构建Wheel/sdist；两个独立`python -I`消费者启动真实产品CLI和SDK OS管道，
  Key后端不是fixture。查询重开保持五账本原字节和原Key身份；三类损坏及两类Legacy共5场景在开放协议前拒绝。
- 发行物复用本机依赖缓存，不声明干净机器安装、三平台、可复现构建或正式发布；真实模型请求0次。
- 713个既有验证文件原字节不变。用户未跟踪安全草稿未运行、未修改、未打包、未计入通过。

## 4. 完整回归、CI及发布边界

完整回归尚待固定源码执行，本报告为草案；不把尚未执行的结果作为通过。
CI仅在本地批量提交后推送一次并后台检查精确HEAD，不逐提交等待。
认证不等于公开授权；POSIX私有文件不声称密文或不可导出Keyring。
不包含同UID任意代码隔离、管理员防御、物理DB身份、完整有效库回滚或整Thread共同删除检测。
5秒为Key准入期限；OS任务结算可延后，不宣称硬抢占文件IO。
密钥恢复及维护CLI、Artifact持久正文、全部Provider/Owner/SDK/MCP、三平台部署、Beta和成本继续开放。
