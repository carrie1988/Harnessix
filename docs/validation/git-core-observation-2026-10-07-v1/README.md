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

# 完整用户观察进入 Git Core2 与原 CAS 验收

## 1. 背景、目标与当前架构

[详细设计](../../changes/m09-r4-git-observed-core.md)定义独立Core2/Plan2。唯一完整
UserObservation包含Source2/Baseline2及全部父引用、common/admin、物理Index、配置摘要和
实现配方；全部事实进入原完整内容地址和原资源属性，不拆成旧Core1或补签缺失字段。
原深重建、规范codec、CAS、Route和材料算法共享，原512KiB等全部上限不变。

Core2写入→原CAS耐久确认与精确回读→原Route寻址恢复→全对象/父历史/Mutation复核
→同次完整Diff构成数据与IO闭环。CAS与Route不构成跨库事务，可能保留无授权材料。
数据、内容地址及材料结果均不是MAC、归属、批准或执行权。

## 2. 测试方法与实际结果

新合同与真实原CAS的468项定向用例通过。该集合使用纯声明
UserObservation和完整CAS fixture，独立字段JSON Oracle验证规范字节与全部指纹；
不声称身份来自真实Session或用户批准。Windows参数只验证声明，不代表原生Windows。

同一wheel安装到独立目录，使用既有受管测试运行时，不代表新建全依赖环境。关联5791项通过、27项条件跳过；原认证SDK
87项通过。关联与SDK实际Harnessix导入全部来自安装目录，候选src不在路径中。
原认证SDK使用离线Provider和实际Session/Store/Patch/Git只读链，验证原观察兼容，不代表
默认Git计划到执行已接通，也不代表真实大模型编码质量。

原候选治理1413项通过；治理按设计加载治理脚本和来源模块，采用原source入口，
不冒称安装隔离。全部485个Python模块在主源码、候选、wheel与安装目录逐字节相等。
原277份Schema字节不变，新增Core2/Plan2两份，原生成器241份受管输出匹配。
Ruff、格式、Mypy、原可读性和文档策略通过；三张图均渲染并查看完整像素。
上述集合独立说明，不累加为覆盖率、平台验收或商业发布结论。

[结构化结果](result.json)、[源码输入](source-inputs.json)、[审查包](review-packet.md)、
[完整性清单](SHA256SUMS)为公开复核入口。原失败、完整XML、实际导入位置、wheel和图像
保存在受限验证目录。验证包自排除，不循环证明自身来源。

## 3. 失败、恢复、安全与部署

错代际、缺字段、子类、容器、重复键、非规范字节及完整材料损坏均拒绝。取消和超时
保留调用方同一异常实例，包括检查点自身抛同类Upstream标记的直传及再次包装路径。
原public snapshot/codec/load边界保留，恢复返回值须重新完整验证，不私有旁路替换。

没有数据库迁移、第二存储、网络服务、用户密钥读取、付费模型请求或预算调整。
沿原Python包和SQLite/CAS部署；旧Core1/Plan1入口及字节保持，不自动升级历史。
Core写后失败可能留下孤儿，不登记业务成功。只读重开恢复原完整正文，不重新观察U补字段。

## 4. 风险与发布边界

原UserObservation仍不是跨Git/数据库原子快照；观察返回期间或之后的外部HEAD/config/Index
变化必须由真实Executor执行前重新核验并拒绝陈旧计划。合同完整持久化不关闭此窗口。
默认Planner/Review/Executor、原批准backref、ProductLink/Bridge、A/T2/D、Commit、Backup2、
真实R3、三平台和同候选有限Beta仍未完成。本组件通过不关闭商业1.0发布门禁。
