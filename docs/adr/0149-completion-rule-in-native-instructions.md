---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [product_config]
related_adrs:
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
  - docs/adr/0148-interactive-run-time-checkpoint-and-wrap-up.md
related_tests:
  - tests/product_config/test_zero_config.py
supersedes: []
---

# ADR 0149 本机模式指令的完成规则

- 状态：评审中
- 日期：2026-11-12

## 背景

负责人的真实试用里（从零创建一个演示黑洞的3D网页），网页在第一轮就写出来了，任务却又运行了一个多小时：
模型一直在自己追加验证和修改，先后运行了43条命令——没有浏览器可用，它就写脚本检查HTML结构、统计括号、
搭建本地服务，再根据这些检查继续改页面。本机模式的系统指令只写了“交付前复核改动、报告已做的检查与缺口”，
没有写什么时候算做完，也没有写无法验证的要求该怎么办。

## 决策

本机命令的系统指令升为`harnessix.native-execution-instructions/v5`，在“交付前复核”之前加入一段完成规则：

> Do what the request asks and stop there: do not add features, files, checks or tooling the user
> did not ask for. Verify with the checks the project already has, once; after they pass, do not
> verify again. When a requirement cannot be checked with the available tools (for example how a
> page looks in a browser), do not build new tooling to check it: name it in the final answer and
> let the user confirm. Once the requested work is done, give the final answer right away.

四层意思：只做被要求的事；用项目已有的检查验证一次，通过后不再重复；用现有工具无法验证的要求不为它造工具，
写进最终回答交给用户确认；做完立即给出最终回答。

## 取舍

- **只改本机模式的指令。** 这是真实用户使用终端界面和`harnessix exec`时的指令；容器命令与外部边界（评测使用）
  的指令不变，它们的行为与评测结果不受影响。是否同步到另外两套指令，待评测确认对解决率没有负面影响后再定。
- **规则写的是“项目已有的检查”。** 项目有测试时仍然要跑；规则限制的是通过之后的重复验证，
  以及为无法验证的要求另造检查手段。检查失败后的修改与重跑不受限制（“通过后”才停止）。
- **靠指令约束，模型不一定每次都遵守。** 模型仍然停不下来时，由用户用一键收尾或运行时间检查点结束
  （[ADR 0148](0148-interactive-run-time-checkpoint-and-wrap-up.md)）。宿主定期提醒模型收尾的方案暂不实施，
  先看本规则的实际效果。

## 后果

- 本机模式下，任务做完后模型应当直接给出总结，并在总结里列出自己没能验证、需要用户确认的部分。
- 系统指令的正文变了，服务商一侧的提示缓存要按新的前缀重新建立；会话记录与配置没有兼容影响。
