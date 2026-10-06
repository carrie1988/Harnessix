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
  - tests/product_config/test_git_delivery_plan_review.py
  - tests/delivery/test_git_parent_closure.py
supersedes: []
---

# 完整 Git 产品执行意图审查包

## 设计与实施边界

完整 Core→原 Route→Artifact 引用封套保持单向指纹。对象范围完整，无占位绑定、伪造阶段或新对象平台。
构造/解码/材料校验不授予归属、批准或执行权限。正式 ProductLink、Artifact 正文/Session/Router、
新批准、NativeBridge、A/T2/D 与 Backup2 为后续必要项；未宣称 R4、R3、平台 Beta 或商业1.0完成。

## 审查发现与闭环

| 问题 | 根因与修复 | 回归证据 |
|---|---|---|
| 任意额外资源 | 完整集合必须等于唯一 Core 资源，不只检查包含 | review-red.xml → review-green.xml |
| 等价UTC时刻不同偏移 | raw commit仍包含偏移；输入与Spec ISO精确一致 | 同上 |
| Checkpoint变更摘要未绑定 | 按原算法重算全部净Mutation摘要 | 同上 |
| 作者NUL | 输入和原Spec前置拒绝，原完整对象解析合同不变 | 同上 |
| JsonValue/AwareDatetime实际类型 | 只认原确切JSON别名和原日期类型，深层容器重新生成 | plan-initial.xml及后继 |
| after目录误用全对象范围 | 区分完整catalog与选中普通blob镜像；不放宽旧算法 | 同上 |
| Core类治理超限 | 提取唯一跨字段算法，原上限不变 | governance-initial.xml及后继 |

## 权限、容量和回滚

不读取凭据、不调用百炼、不改费用预留或定时任务。旧272Schema、Native18/27 selectors/13 Hooks、
20/45/240/300秒、256资源、8MiB对象、32MiB镜像、512KiB记录不扩大。
新组件未暴露默认工具，无外部Git副作用或数据库迁移；代码回退不需要回滚用户Index/Ref/文件。
原失败、独立审查结论、同候选安装证据分别保存，不用绿测替代实际生产闭环。

## 历史 Native18 断言修复

完整安装回归第一次1152通过、1失败。失败位于开工基线中未更改的历史测试：
它将d7e8668→f7d06e2的单Git文件四叶变更与当前bea5718两输入八叶混合。
断言改为固定f7d06e2661b4ce91225790edd06a52298446c225历史Meta/parser/全部18Source字节，
并额外使用原live精确八叶核验。当前contract.json/parser/CI门禁没有修改，失败日志和XML保留。
源代码wheel完全不变；只纠正时间基线，未新增Selectors、降级容量或跳过测试。
