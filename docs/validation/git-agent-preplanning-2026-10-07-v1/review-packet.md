---
doc_type: validation-evidence
status: current
version: 1
code_revision: 3443422cc83f804b7c6d09e41ac2645c0a17c1b1
owners: [core]
modules: [trusted_actions, product_config, delivery]
related_adrs:
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/trusted_actions/test_agent_preplanning.py
  - tests/product_config/test_git_delivery_core_store.py
  - tests/product_config/test_git_delivery_route_core.py
supersedes: []
---

# 审批前可信准备与完整 Git Core 恢复审查包

## 1. 审查范围及架构决策

[详细设计](../../changes/m09-r4-git-agent-preplanning.md)将宿主注册的可信准备阶段置于原 Gateway
首次正式规划之前。准备结果不得覆盖 Policy、Sandbox、Context、Executor 或批准；没有准备器
的定义仍使用原同步规划。准备后的实际原生 Snapshot 必须与完整预期值一致，不能用文件摘要
替代对象身份和父历史。完整 Core 使用唯一原 Workspace CAS，不增加第二个 Store、SQL 表或签名。

Core 保存和原 Route 持久化并非跨库原子事务。取消、写入后回读失败或尚未建立 Route 时可能
遗留合法无授权 CAS 内容；这些内容不能登记为业务成功，也不得用重算当前工作区的方式补造原意图。
重启恢复首先查询原 Route，再从唯一资源寻址完整 Core。完整资源、原确定性身份和全部交叉字段均核验。

## 2. 问题、根因及回归闭环

| 发现 | 根因与处置 | 保留证据 |
|---|---|---|
| 并发已有 Route 的 Workspace 请求未完整匹配 | 仅匹配规范资源不足以发现同资源摘要下的不同请求；增加全部原请求及 Snapshot2 精确匹配 | preparation-initial.xml、preparation-second.xml |
| 异步准备后重复调用 Decoder | 准备时规范化一次，同步入口再规范化一次；共享唯一规范化后规划算法，最终 Route 与准备参数保持一致 | decoder-red.xml、decoder-green.xml |
| 原 Store 与本次调用的控制异常来源混淆风险 | 不按异常类型、错误码或堆栈猜测；原 CAS 增加可选单次检查点，仅真实检查点以原控制标记穿过 IO 错误转换 | core-store-agent 中独立审查与完整回归 |
| 系统 Git 无 SHA256 初始化及隔离配置支持 | 系统 Git 2.24.3 不满足原测试环境，使用现有 Git 2.53.0；测试、原上限和门禁均未放宽 | governance-initial.xml、git-environment-reproducer.json、governance-delivery.xml |
| 设计章节未匹配正式文档分类 | 补充明确数据结构章节，不改变治理策略 | docs-initial.json、docs-package.json |
| 时序图标签分号导致解析失败 | 标签改为合法文字分隔并重新渲染，检查实际完整图像 | diagrams/2-initial.log、diagram-proof.json、diagram-visual-review.json |

失败证据和首次候选安装结果完整保留；最终验收使用修复后同一候选的安装包，不以首次绿测
代替最终源码验收。各测试集合有重叠，不将数量相加为覆盖率。

## 3. 数据、安全及控制边界

- 完整 Core 规范正文仅排除自身 fingerprint，原始 SHA 等于 Core 内容地址；父 Manifest/Chunk、对象范围、
  十六进制对象字节及全部嵌套指纹保持完整。完整 Plan 原编码字节兼容；两者仍受原 512KiB 上限约束。
- 拒绝重复键、额外字段、默认补全、别名、非规范同义字节、子类、伪造容器、摘要不符及读取损坏。
- 原 CAS 写算法、耐久确认、只读权限、固定路径和内容上限不变；不修改共享检查点，不使用堆栈内省。
- Turn 取消、父 Task 取消及原期限异常传播并回收托管任务；普通底层错误只公开有限分类，不泄露路径或正文。
- 原 Native18、27 selectors、13 Hooks、20/45/240/300 秒、256 资源、8MiB 对象、32MiB 捕获、
  512KiB 记录及 Root/Owner/Scope/MAC/Lease 门禁保持原标准。
- 不读取凭据、不调用模型、不调整费用预留或定时任务；不改变 Docker 或其他服务运行状态。

## 4. 安装、回退及验收限制

沿用原 Python 包和本地 SQLite/CAS 部署，不增加网络服务、安装渠道或迁移。实际测试构建
wheel 后在独立目标目录安装；关联测试的所有 Harnessix 模块须来自该安装包，不能从候选 src 旁路导入。
代码回退不需要回滚用户 Index、Ref 或文件：本组件没有默认 Git 工具或外部 Git 写效果；留下的
无授权 Core 内容不等于用户数据损坏，不能擅自删除。

真实 Stores 关闭重开、注册 Gateway 与完整材料测试不等于实际认证 Session、用户批准、完整
Review Artifact 或 Git 业务执行。Windows 声明仅作逻辑合同回归，不构成 Windows 原生验收。
实际 Planner/Executor、ProductLink/NativeBridge、A/T2/D、独立 Commit、Backup2、R1～R6、
真实编码质量和同候选有限 Beta 仍为发布必要项。该组件通过不表示商用 1.0 已完成。

[结构化结果](result.json)、[来源哈希](source-inputs.json)、[制品完整性](SHA256SUMS)提供复核入口。
