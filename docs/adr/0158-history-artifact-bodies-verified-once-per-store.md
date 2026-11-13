---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [artifacts, agent]
related_adrs:
  - docs/adr/0057-tool-result-model-view-and-artifact-binding.md
  - docs/adr/0097-typed-binary-publication-and-owner-protection.md
related_tests:
  - tests/artifacts/test_verified_history_bodies.py
  - tests/artifacts/test_batch_verify.py
  - tests/artifacts/test_model_history.py
supersedes: []
---

# ADR 0158 历史Artifact的正文在同一存储对象里只完整核对一次

- 状态：评审中
- 日期：2026-11-13
- 修订：[历史Artifact批量验证](../changes/m09-3d-history-artifact-batch-verification.md)第3.2节的非目标2、3

## 背景

每次请求模型之前，运行时要在5秒内核对历史里引用的全部Artifact（`agent/runtime.py::_verify_history_artifacts`，
`artifacts/batch_verify.py::verify_references`）。每一条都做：记录归属与用途、来源认证、清单一致、是否过期、
正文SHA-256、记录解析与记录数、命令输出的正文校验、公开输出的密钥扫描、分页与省略覆盖。

0.10.0b24的Beta复测（GitHub Issue #8）：一个57步的长会话反复运行数百项测试，约10分钟后以
`context_artifact_timeout`中断，交付没有完成。本机测量：除密钥扫描之外，80个工具结果一次核对约8毫秒；
密钥扫描约11 MB/s，单份正文最多1 MiB。所以每步的核对耗时与会话里累计的命令与工具输出量成正比，
累计到几十MB就超过5秒——而且这些正文每一步都被重新扫描，哪怕自上一步以来一个字节都没有变。

原设计（m09-3d）有意不放宽时限、不做跨步骤缓存：“超出预算的历史规模仍失败关闭，本设计不承诺无限历史”，
“TTL与外部清理必须按现状被每次验证发现”。负责人在三个选项（只完整核对一次、把时限放宽到30秒、维持现状）里
选了第一个。

## 决策

1. **同一个存储对象完整核对过的引用，之后只再确认正文还是同一份。** 每一步照旧核对：记录存在且属于这个
   作用域、归属与用途、来源认证（发布签名）、清单与已提交的引用一致、没有过期、状态为已发布、正文大小与SHA-256。
   跳过：记录解析与记录数、命令/动作输出的正文校验、公开输出的密钥扫描、分页与省略覆盖。
2. **记住的是一条引用的完整身份**：作用域、归属会话、调用、用途、省略字段与整份清单（含SHA-256、记录数、
   过期时间）。任何一项不同都按首次核对。
3. **记录挂在存储对象上**（弱引用），与它的公开输出保护同生命周期；进程重启或换一个存储对象都从头完整核对。
   每个存储对象最多记住4096条，超出时全部忘掉。
4. **错误码与顺序不变。** 过期仍是`artifact_expired`，正文被改仍是`artifact_corrupt`，记录被删或换作用域
   仍是`artifact_not_found`。单条路径（非SQLite的验证器）不变。

## 取舍

- **跳过的检查为什么安全。** 被跳过的都是“正文的函数”：同一份字节（SHA-256每步确认）在同一个保护范围
  （存储对象的生命周期内不变）下，记录解析、密钥扫描的结果不会变；分页与省略覆盖比对的是已经提交、
  不会再改的工具调用与结果。真正可能在步骤之间变化的——记录被清理、过期、被篡改、作用域变了——每一步仍然核对。
- **SHA-256仍是线性的。** 但它约2.8 GB/s，比密钥扫描快约250倍：累计32 MB输出时每步约11毫秒。
  历史规模的上限仍然存在，只是从“几十MB”退到“上百GB”量级，实际先被上下文窗口与压缩约束。
- **没有放宽时限。** 5秒仍是“本机核对异常慢”的信号；放宽只会让每一步都可能白等几十秒。
- **内存。** 每条记住的身份是一行几百字节的字符串，4096条约1 MB。

## 后果

- 长会话里每步请求前的核对从随输出增长、可达数秒，降到毫秒级；Issue #8这类中断不再因为输出累积而发生。
- 首次核对（每个进程里每份正文第一次出现在历史中）的开销与此前相同。
- `docs/changes/m09-3d-history-artifact-batch-verification.md`的非目标2、3按本ADR修订。
