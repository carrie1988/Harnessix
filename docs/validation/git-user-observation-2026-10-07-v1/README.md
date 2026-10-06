---
doc_type: validation-evidence
status: current
version: 1
code_revision: db160c8caeafb0385a2362ccd0af1502a1f6e79e
owners: [core]
modules: [product_config, session, workspace, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_user_observation.py
  - tests/product_config/test_git_user_observation_controls.py
  - tests/product_config/test_git_parent_consumers.py
  - tests/session/test_authenticated_history.py
supersedes: []
---

# 用户 Git 完整只读观察验收

## 1. 范围与架构决策

[详细设计](../../changes/m09-r4-git-user-observation.md)将用户脏工作区U的完整观察与干净私有锚A
准入分离。原认证Session、实际Router/CAS/Scope及固定GitReader共同冻结；实际完整认证历史
支持唯一Source2捕获，唯一原基准算法与目录、物理Index、配置值及末端历史核验共同形成结果。
完整新观察是数据合同，不是批准、MAC或跨数据库与文件系统原子快照。

原Core1不携带此完整观察，正式规划后续必须显式绑定完整新代际，不能偷换配置名称字段。
该组件不注册默认Git工具，不创建A/T2/D，不提交Git效果，不关闭任何商业发布门禁。

## 2. 验证方法与完整性

使用普通隔离候选副本构建wheel并安装到独立目录；所有Harnessix导入来自安装包，测试根
不包含候选src。源码、候选、wheel和安装目录的全部Python模块逐字节相等。
实际SDK使用离线ScriptedProvider，Session认证、原Stores、Patch审批与Git读为真实集成。
离线模型不证明真实LLM编码质量，模拟Windows转换器不证明Windows原生业务验收。

[结构化结果](result.json)、[来源哈希](source-inputs.json)、[审查包](review-packet.md)和
[完整性清单](SHA256SUMS)构成公开复核入口。完整XML、失败记录、源码输入、实际导入位置、
渲染图及安装物保留于受限验证目录；公开结果不包含配置、Index正文、路径错误或凭据。

最终同候选安装验证：新认证SDK87项通过；关联1559项通过、4项原生Windows条件跳过；
治理1413项通过。全部483个Python模块四方逐字节相等，原276份Schema保持，新增1份
用户观察Schema。Ruff、格式、Mypy、可读性与文档治理通过；三张图均渲染并查看完整像素。
上述集合独立说明，不累计为覆盖率。

## 3. 安全、失败、恢复与部署

调用方控制异常经原生边界包装和解包保持来源；共享期限、取消及原回收语义不放宽。
实际历史、来源、目录、Index、HEAD、配置值或宿主发生漂移则拒绝，不修复历史或签发批准。
最后Git await在最后认证历史之前；之后仅同步验证来源与Index，返回后外部修改仍可能发生。

原Scope生命周期受核验，但POSIX原Git输出端口不消费redaction。完整私有配置仅用于SHA，
不返回或保存正文；不声称已脱敏。七文件实现摘要仅证明产品层配方，全依赖一致性另行验证。
原Native18、27 selectors、13 Hooks和全部原资源/时间上限不变。

沿用原安装和SQLite/CAS，不新增服务、网络、迁移或安装渠道。原CAS可能追加无授权来源
材料，失败不产生Git效果；重开必须重新认证并观察当前U。代码回退不需要改动用户Git数据。
没有新付费模型请求、凭据读取、预算调整、Docker或定时任务变更。

三次交叉读取仍存在最后H3读取期间或其后的HEAD/config独立变化窗口；它不是
跨库原子快照，不能仅凭观察值发布Git效果。后续实际执行必须重新核验当前
HEAD、配置、来源和物理Index，并按原失效语义拒绝陈旧计划。
