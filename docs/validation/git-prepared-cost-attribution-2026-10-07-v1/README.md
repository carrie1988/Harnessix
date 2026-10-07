---
doc_type: validation-evidence
status: current
version: 1
code_revision: b3760f560d2e83f85bc9d880cd9f8b0b0893ad4c
owners: [core]
modules: [product_config, workspace, trusted_actions, agent]
related_adrs:
  - docs/adr/0007-agent-loop-and-cancellation.md
related_tests:
  - tests/product_config/test_git_prepared_link_controls.py
supersedes: []
---

# 原安装产品 Git 待审批完整回读成本归因

判定：`INSTALLED_PREPARED_COST_ATTRIBUTED_RESPONSE_P1_OPEN`。[完整研究](../../research/git-prepared-cost-attribution.md)说明当前源码、调用流程、接口与字段、归因方法、失败语义及未采用方案；[结构化结果](result.json)固定观察数值与验收边界。

## 1. 输入及实际结果

macOS arm64 / CPython 3.12.7；基线 `b3760f560d2e83f85bc9d880cd9f8b0b0893ad4c` 与原安装 Wheel 的548件产品包成员逐字节一致。Wheel摘要为 `fd56221c5973ce2da12e3e9a5dfeb3ebc6836a7ed0a3c6e1b60b055c76e66dcd`，全部 Harnessix 模块来自隔离安装，未导入修改后的源码。

实际 SDK 仅运行 `test_physical_database_file_replacement_after_await_rejects_same_rows`：1通过、0失败、0错误、0跳过，约102.05秒；保留1项插件预导入警告。50 ms心跳76个观察，最大间隔20.761515秒。该测试通过仅证明限定物理替换负控，不证明正常响应性。

高频计数：短只读Owner观察395523次，打开连接395523次，完整Owner查询791051次。计时嵌套，不得将其总时长相加。区间分析通过裁剪及并集合并去重；分析器最终CPython3.12.7的26项合成测例通过，但不是新增产品用例。初次3.8.2隔离启动的26项导入错误及后继正确启动记录保留，不算产品RED。最长窗口已解释99.6718%；仅输入的10个最大窗口并集解释87.3082%，其余不归入任何未计时函数。

## 2. 未取得的结论

未改产品源码、Schema、依赖、锁、期限或容量。连接复用不能证明原失败语义等价，不采用；分层检查点为待决提案，未实现、未取得验收。无插桩SLA、原生三平台、完整Git效果、Commit、Backup2、真实R3、独立Beta和商用R1～R6仍须独立完成。

模型请求0，单人先导真实任务0；两笔未决费用保持原预留，旧质量报告和旧失败原件不改写。不把不同装配/插桩的最大间隔相减宣称性能改善，不关闭同步响应性P1。

## 3. 完整交付与复核

本地交付标识 `git-prepared-cost-attribution-20261007-v1` 包含完整冻结源码、原Wheel与来源摘要、诊断脚本及原日志/XML/JSON、安装模块来源、区间分析与测例、三份渲染图、正式报告、Review Packet和Manifest。

其中原锁安装输入仅保留复用来源，仍指向前一固定安装；不得视为本次新环境的自动安装收据。Manifest验证用于检查交付字节，不能代替业务或商用验收。
