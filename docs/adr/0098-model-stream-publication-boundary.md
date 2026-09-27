---
doc_type: adr
status: draft
version: 1
code_revision: pending
owners: [core]
modules: [agent, secrets, product_config, app_server, session]
related_adrs:
  - docs/adr/0097-typed-binary-publication-and-owner-protection.md
related_tests:
  - tests/agent/test_text_publication.py
  - tests/agent/test_model_publication_runtime.py
  - tests/product_config/test_publication_scope.py
supersedes: []
---

# ADR-0098：模型原文流式发布与事件持久前边界

## 状态与背景

采用当前增量设计；历史Session、跨重启公开授权和整体0.9发布仍独立开放。
已登记材料可以由模型直接输出，经即时通知、事件持久化和后续历史传播。
仅工具结果或Artifact保护不能关闭这个出口；最终文本检查晚于Delta队列。

## 决策

建立可选纯`PublicTextOutputProtection`/`PublicTextStep`能力；已配置公开保护但缺少流式能力默认拒绝。
每步骤共享输入字节和模式工作预算；每content_id独立尾窗，保留最长模式前缀并对齐UTF8，
扫描后返回原文安全前缀，不改写正文。单次同步检查10秒，不把正常网络等待算入该期限。

提取`ModelTextPublication`维护文本生命周期和原文一致性；Runtime在原`_commit`事件CAS前检查完整批次，
并在主/摘要模型请求消费前检查完整出站请求。保持尝试意图先于HTTP、已知用量、原工具身份、Hash与Schema。
完成事件提交成功才发布最后尾部；异常或取消关闭流和窗口，固定失败不携带原始诊断。

## 备选与后果

不使用整回答缓冲、独立Delta扫描、UI过滤、跨content_id拼接或事后改写Hash。
安全前缀仍可渐进输出，通知数量和分块不必与Provider相同；极端碎片会消耗累计扫描预算并可明确拒绝。
Scope仅覆盖登记原值及有限编码，不宣称DLP、不可变内存擦除或同步硬抢占。

旧历史当前值出站检查不等于旧正文授权，直接`store.append`用户入口仍须独立治理。
无新服务、数据库迁移、公开DTO或外部依赖。完整架构、流程/时序/数据流、接口、字段、
伪代码、失败恢复、测试和源码导航见[完整详细设计](../changes/m09-4a-model-text-publication.md)。
