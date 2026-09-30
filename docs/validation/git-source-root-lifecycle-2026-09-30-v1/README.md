---
doc_type: validation-evidence
status: current
version: 1
code_revision: b6810c92f996aeb6283c9ab284a4cc91f9e58e14
owners: [core]
modules: [product_config, workspace, tools]
related_adrs:
  - docs/adr/0080-capability-proven-product-action-composition.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_delivery_source.py
  - tests/product_config/test_git_delivery_source_sdk.py
  - tests/product_config/test_product_patch_rollback.py
  - tests/product_config/test_product_rollback_sdk.py
  - tests/delivery/test_rollback_binding.py
supersedes: []
---

# 产品Git来源根目录生命周期验证

## 1. 结论与适用范围

生产模块固定为`b6810c92f996aeb6283c9ab284a4cc91f9e58e14`，本专项只修改测试与设计材料。
活动Windows产品禁止替换根目录；退出后新根即使最终文件字节相同，也不能继承原来源身份。
[完整设计](../../changes/m09-r4-product-git-delivery-source.md#9-windows原生根目录生命周期验证)
说明需求背景、源码依据、架构与流程、接口、伪代码、失败、持久化、安全、部署及测试边界。

**本机验证通过；修正后的Windows原生断言待执行。R3、R4及商用1.0不关闭。**

## 2. 原生失败与根因

[CI 36731845536](https://github.com/carrie1988/Harnessix/actions/runs/36731845536)的固定候选
`b6810c9`在Windows第一组实际为126通过、1失败、2跳过，运行117.45秒。
此前LF夹具修正后的审批与回滚场景已在该组执行，不能仍沿用前序45项失败描述当前候选。
唯一失败是来源测试在活动产品内直接执行根目录重命名，系统抛出`WinError 32`。
此时Reader尚未执行，不能把失败解释为来源身份校验漏拒绝。

[`WindowsWorkspaceRoot._open`](../../../src/harnessix/workspace/windows.py)只允许`FILE_SHARE_READ`，
[`CodingToolRuntime`](../../../src/harnessix/tools/runtime.py)在整个产品生命周期中持有根端口。
活动根被保护是既有行为，不允许添加共享删除标志、修改生产句柄或取消根身份校验来满足夹具。
原生后续raw/Git步骤均跳过；Linux两Job的许可证失败与Windows该失败分开记录，
macOS、文档和容器Job成功不等于全CI成功或后继原生步骤已执行。

## 3. 实施与测试合同

[`原来源测试`](../../../tests/product_config/test_git_delivery_source.py)保留五类第三内容/根场景。
Windows根场景执行真实rename并要求错误码32，同时要求目标目录未出现、原文件和事务不变、
来源仍绑定原根身份和事务库零写入；POSIX仍真实替换活动根并验证来源拒绝，不增加跳过。

新增退出后负控在全部平台执行：实际关闭产品、真实rename旧根、创建同路径不同根、
复制相同最终字节；重开原Plan/Audit/Transaction，使用原Thread和原事务ID。
在任何文件正文读取之前必须返回`git_delivery_source_changed`；两套文件字节与原Route/Transaction不变。
该测试没有重新运行Patch，没有构造新批准，没有补签旧事实。

## 4. 分层测试结果

| 运行环境 | 结果 | 验证边界 |
|---|---|---|
| macOS Python3.12.7 | 69通过、0跳过 | 原来源、连续修改、根替换、正式Patch/回滚及SDK关联 |
| macOS Python3.13.8 | 69通过、0跳过 | 同一选择器，独立Python运行 |
| 实际源码外Wheel Python3.12.7 | 69通过、0跳过 | 禁用源码pythonpath，从site-packages导入实际产品 |
| 原Windows Server CI | 126通过、1失败、2跳过 | 修正前实际原生事实；后续步骤跳过 |
| 修正后Windows Server CI | 待验证 | 活动根保护与退出后根拒绝均须实际通过 |

上述69项运行互相重叠，不能累加为207个独立场景。
源码外运行复用已冻结的真实Wheel，SHA为
`9e7a0883715853d4c38c1a0f25a6e92ad9e63a689e493a29f84abbe11cf4061c`；
409个生产Python模块在当前源码、Wheel和实际安装路径逐字节一致，无源码导入。
该Wheel是内部`1.0.0rc1`，版本号不代表商用验收。
治理302项通过，原可读性策略和Ruff/格式检查通过；Secret扫描3775个输入、零命中。
差异文档门禁和三幅变更文档图均实际渲染，新增生命周期图已检查可读性。
结果另见[测试记录](test-results.json)；不以静态检查替代原生运行。

## 5. 原件与可追溯性

私有原件逻辑位置为`Harnessix/verification/git-source-root-lifecycle-20260930-v1`，
原CI日志保存在`Harnessix/verification/windows-raw-receipt-20260930-v1/ci-36731845536`。
目录0700、文件0600；公开材料只保存固定系统错误、统计和摘要，不包含路径正文、秘密或原模型内容。
[事实](facts.json)、[源码与测试绑定](source-bindings.json)、[测试结果](test-results.json)、
[Review Packet](review-packet.json)和[Manifest](manifest.json)共同定义验证边界。
旧失败日志与旧交付材料保留，不用后继通过覆盖。

## 6. 兼容、风险与后续验收

生产代码、Schema、数据库、依赖和预算均未改变；无需数据迁移或新增中间件。
没有模型请求、凭据读取或实际费用账本登记，R3真实成绩保持。
既有取消、超时、换行拒绝及文件事务语义仍由原回归保护。
Windows Server结果不能替代Windows11消费者OS、完整编码、升级恢复或独立Beta。
修正后原生结果取得前不称Windows已验收；完整Commit/Checkpoint产品接线和R1～R6仍开放。
