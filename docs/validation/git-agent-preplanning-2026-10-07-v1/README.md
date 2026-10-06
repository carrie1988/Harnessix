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
  - tests/delivery/test_store.py
supersedes: []
---

# 审批前可信准备与完整 Git Core 耐久恢复组件验收

## 1. 验证范围

验证宿主注册准备器接入真实 Agent Gateway 首次规划之前，保留原参数规范化、Policy、
实际 Snapshot 和持久 Route；已有计划查询优先。完整 Core 在原 Workspace CAS 耐久写入、
完整回读，通过原 Route 唯一资源与确定性身份寻址，并复用全部原交叉字段校验。

当前集成使用真实原 Stores、完整材料及注册 Gateway；其 Session、批准和 Review 场景为
组件测试，不是实际认证 Git 业务装配。默认 Git 工具、独立批准、ProductLink/NativeBridge、
A/T2/D、独立 Commit、Backup2 和商业发布均不在已完成范围。
完整架构、三张图、源码位置、接口、字段、核心伪代码和失败语义见[详细设计](../../changes/m09-r4-git-agent-preplanning.md)。

## 2. 同候选验证结果

- 最终安装 wheel 关联回归：2475 通过，0 失败/错误/跳过，453.675 秒。
- 原治理回归：1413 通过，0 失败/错误/跳过，40.239 秒。
- 479 个源码模块与主工作区、候选、wheel 和安装目标逐字一致；实际 Harnessix 导入均来自安装包。
- Ruff、格式、479 源码模块 Mypy、原可读性策略及文档门禁通过；276 个原 Schema 字节不变。
- 原 Native18、CI、治理策略完整文件字节及三个原 CAS 写算法 AST 不变；三张图已渲染并视觉核验。
- 完整 Secret 扫描覆盖 4980 个输入，固定规则零命中；未调用模型或读取凭据。

全部测试由同一最终候选和安装包执行，不合并重叠集合为覆盖率。Windows 仅验证逻辑合同
声明，没有形成 Windows 原生验收结论。测试原参数、容量、期限、原生门禁均未放宽。

## 3. 失败保留与审查闭环

并发 Workspace 请求遗漏、Decoder 重复规范化、系统 Git 环境不兼容、文档章节和图形解析
问题均保留首次失败及修复后证据。首次安装包通过仍不代替 Decoder 修复后的最终安装包复验。
完整原因、修复边界和回归证据见[审查包](review-packet.md)。

[结构化结果](result.json)、[完整输入哈希](source-inputs.json)、[公开制品完整性](SHA256SUMS)
提供复核入口。输入清单记录完整主要代码、测试、Schema、治理、构建配置与本变更设计输入；
公开验收包自身单独校验，避免自引用哈希循环。本地证据另保存 XML、工具结果、所有导入路径、
安装包、图像和完整证据清单，不发布凭据、模型响应或用户源码正文。

## 4. 风险、剩余验收及发布结论

CAS 内容地址只证明字节完整，原 Route 一致性也不授予 Session 归属、MAC、Owner 或批准。
取消与跨 Store 中断可能留下无授权 CAS 内容；必须失败关闭，不能追认成功或重算旧交付意图。

实际 Git Planner/Executor、认证 Session/Review/ProductLink、NativeBridge、新 A、prepared T2、
D 物化及独立 Commit、业务 Backup2、恢复后新根重绑定、R1～R6、真实编码质量、三平台实际
安装消费者与同候选有限 Beta 均未完成。本报告为组件验收，不发布或宣称商用 1.0。
