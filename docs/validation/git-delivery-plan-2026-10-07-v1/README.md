---
doc_type: validation-evidence
status: current
version: 1
code_revision: bea57181dc5991cb69f3beb55f2fdfb21ca4c75b
owners: [core]
modules: [product_config, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_delivery_plan_contracts.py
  - tests/product_config/test_git_delivery_plan_materials.py
  - tests/product_config/test_git_delivery_plan_review.py
  - tests/unit/test_git_inventory_scope_contracts.py
  - tests/unit/test_git_inventory_scope_materials.py
supersedes: []
---

# 完整 Git 交付计划组件验证

## 验证范围

验证完整 Core→原 Route→Review 引用封套、严格512KiB规范字节、无绑定八字段对象范围、
实际原CAS两树和父历史、完整目标/Diff/Commit编码，以及原目录兼容性。
本验证不包含默认Git工具装配、新批准、认证ProductLink/NativeBridge、A/T2/D写阶段、
Backup2或商业发布验收。完整设计见[详细设计](../../changes/m09-r4-git-delivery-plan.md)。

## 证据与限制

验证结果由同一候选源码和安装wheel记录。失败记录完整保留，不覆盖旧失败或改变预算。
Windows只验证逻辑元数据，未产生原生平台结论。未使用模型请求、Keychain或调整定时任务。

## 同候选验证结果

- 安装wheel关联回归：1153通过，0失败/错误/跳过，402.256秒。
- 治理回归：1413通过；Ruff/格式、475源码模块Mypy、原可读性规则通过。
- 475个模块与候选wheel逐字一致，实际所有Harnessix导入来自安装包而非clone/src。
- 原272Schema完整字节不变，新增4个正式合同；三张图渲染并视觉核验。
- 原Native18元数据/parser、CI选择器/期限和治理策略未变；Secret扫描完整覆盖4966输入，零命中。
- 初始计划、审查负例、治理及安装回归失败全部保留；历史四叶与当前八叶分别按原精确字节核验。

[结构化结果](result.json)、[完整输入哈希](source-inputs.json)、[审查包](review-packet.md)、
[公开制品完整性](SHA256SUMS)保存可复核边界。以上集合含有重叠，不将测试数相加为覆盖率。

## 剩余验收

实际Artifact/Session/Router归属和独立批准、typed ProductLink、NativeBridge、A/T2/D写装配、
Backup2及R1～R6发布/Beta仍未完成。保持原费用预留，不从组件绿测推导商用可发布。
