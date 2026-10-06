---
doc_type: validation-evidence
status: current
version: 1
code_revision: d8524741e12466a8bade4adc72ad8d3e9222cfe1
owners: [core]
modules: [product_config, delivery, workspace]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_delivery_observed_core.py
  - tests/product_config/test_git_delivery_core_store.py
  - tests/product_config/test_git_delivery_route_core.py
  - tests/product_config/test_git_user_observation.py
supersedes: []
---

# Git Core2 与原 CAS 审查包

## 1. 需求、流程、数据与源码

完整用户观察不适合隐式降格为Core1。独立Core2唯一序列化全部UserObservation，并以四个
非序列化只读property复用原基准算法。Store/Key与观察一致，全部嵌套指纹进入外层内容地址；
原Call、目标、Scope、Commit、Route交叉字段仍由同一原算法校验。

[完整详设及接口](../../changes/m09-r4-git-observed-core.md)列出需求、三张图、类/接口/字段、
伪代码和测试源码映射；[新合同](../../../src/harnessix/product_config/git_delivery_observed_contracts.py)、
[原CAS入口](../../../src/harnessix/product_config/git_delivery_core_store.py)、
[完整材料](../../../src/harnessix/product_config/git_delivery_plan_materials.py)为重点入口。

## 2. 审查问题及闭环

| 问题 | 根因及修复 | 证据边界 |
|---|---|---|
| 原public边界旁路 | 初始共享私有直连绕过原public load；恢复公共snapshot/codec/load并重验返回值 | 原578项与新公共hook负例均复验；保留初始5FAIL |
| Upstream同类型检查点身份 | 先解transport会错误返回调用方marker内层；优先恢复保存的实际异常对象 | 两代persist/load及直传/包装12例，断言同一外层实例 |
| 类体量超原策略 | 两代入口和共用算法集中在类体；原CAS算法移到同文件纯helper，wrapper仅持原Store | 原可读性策略不变，最终生成报告匹配 |
| 正式详设章节缺失 | 内容存在但未形成明确事务和风险语义章节 | 补齐持久化/事务与风险/取舍，原文档规则不变 |
| 治理验证协议不适配 | 首次basetemp含空格触发原Windows预检路径拒绝；治理又按设计加载来源模块 | 改用无空格外部临时目录和原source入口；保留18FAIL，不改产品拒绝规则 |

最终限定10文件静态审查未发现确定must-fix；静态结论与运行测试分别保留。
独立规范JSON Oracle不调用受测codec，固定旧Schema摘要来自原基线，完整512KiB正例及
超一字节负例均实际构造，不靠降低负载、截断材料或提高阈值通过。

## 3. 异常、安全、持久化与恢复

原CAS耐久写/精确回读/完整解码保留；关闭重开与只读加载不补字段，不新增SQL或签名。
Scope全对象与完整Source2父Manifest/Chunk读取后核验净Mutation、Diff及提交正文；同次结果
只是事实数据，不能转授Scope或批准。原Native18、27 selectors、13 Hooks与资源上限保持。

模拟IO控制注入验证异常身份，不冒称未经替换的原生控制全链路；声明Windows参数不冒称
原生Windows平台通过。实际Git执行仍须重验当前U与原Session权威。

## 4. 部署、验收与剩余风险

同wheel安装关联与原认证SDK验证导入隔离和四方485模块字节；治理独立按来源入口执行。
完整manifest、原失败、图像、实际导入与Schema证明留存。零付费模型请求、零用户凭据读取；
离线Provider不证明真实R3。默认Planner/Review/Executor与商业发布全部剩余门禁保持开放。

[结果](result.json)、[来源](source-inputs.json)、[清单](SHA256SUMS)。
