---
doc_type: adr
status: current
version: 1
code_revision: 5402621ab7700f55382d78fa95681dfe3485c960
owners: [core]
modules: [agent, artifacts, processes, secrets, product_config, trusted_actions]
related_adrs:
  - docs/adr/0096-product-credential-and-artifact-publication-boundary.md
related_tests:
  - tests/artifacts/test_binary_publication.py
  - tests/processes/test_output_protection.py
  - tests/product_config/test_process_action.py
  - tests/trusted_actions/test_publication_recovery.py
supersedes: []
---

# ADR-0097：类型化二进制公开与Owner持久前保护

## 状态

接受当前切片的设计与实现；整体公开治理、安全发布和0.9范围仍未完成。

## 背景与决策驱动

正式Process Base64 Chunk的前缀位移与跨块值不能由整值字符串模式证明安全。
模型保护材料未进入Owner捕获前脱敏，Artifact拒绝仍可能发生在敏感字节落盘之后。
[完整详细设计](../changes/m09-4a-typed-binary-output-publication.md)包含背景、目标、模块边界、
四幅架构/流程/时序/数据流图、接口与字段、源码映射、伪代码、失败恢复及平台验收限制。

## 决策

按持久purpose选择两个既有正式Process解析器，原JSON与重建双流使用同一工作预算；
已配置保护但缺少二进制端口时默认拒绝，不解码任意同名字段。
将原模型快照作为保护专用端口传给实际Supervisor，Start v2私有封套携带有界瞬时材料；
原v1不变，材料不增加目标环境注入权限。Owner先流式脱敏，再计量、摘要和写盘。
已确认效果的恢复遇有限公开保护拒绝只返无正文无Artifact元数据，恢复策略明确独立于效果origin。
原Audit、正文、Manifest与Hash不事后重写，未知效果仍禁止自动重放。

## 备选、后果与兼容

拒绝字段猜测、每页/Chunk单独检查、双流拼接、模型key环境注入、持久保存原值或沿用旧Hash改写正文。
父宿主与Owner必须同版本部署；未配置保护的独立宿主仍使用v1兼容行为，不能据此声称默认产品安全。
当前Migration 0028及Epoch语义不变，旧历史和跨重启安全恢复仍待独立设计。
本地真实OS进程测试不代替Windows、实际容器、网络Provider、真实用户安装及整体生产验收。
