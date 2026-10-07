---
doc_type: validation-evidence
status: current
version: 1
code_revision: 38cf8f7f601ce523d3c76b3473cf8d266339ed0d
owners: [core]
modules: [product_config, session, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_decided_source_reader.py
  - tests/product_config/test_git_decision_link_sources.py
  - tests/product_config/test_git_prepared_link_connection.py
supersedes: []
---

# 原资源决定读取评审包

## 变更与来源

生产仅原历史 Reader 新增read_decided；原构造器/read_all/辅助AST保持。返回普通事实，没有执行或发布Token。
[完整详细设计](../../changes/m09-r4-git-decision-original-body-sources.md)与[总体决定方案](../../changes/m09-r4-git-approved-link.md)同步，正式Writer仍planned。

## 验证与失败

主仓451唯一功能节点、19接线与12末端接线短测；最终同源码SDK1PASS、独立85PASS/3FAIL、FD换回、非当前Task持锁证明分别计数。
独立审阅确认两个P2返回对象漂移与合法副本重定向均已整改；最终只交付原上下文退出后核验的新快照，原三FAIL保留；源文件摘要、实际SDK同源或版本差异、费用、环境以及最后独立审阅以[facts.json](facts.json)为准。
初次Apple Git初始化FAIL、格式/导入治理初败和三个真实晚漂移FAIL保持，不通过删除断言或增加期限转为PASS。

## Go / No-Go

只读入口可作为原资源事实依赖。正式Writer、Git效果、恢复屏障、B4/B7/P1及R1～R6：No-Go或仍开放。
不追加Git决定、不默认装配工具、不改变客户原目录或参考件、不计真实Beta接受和商用发布。
