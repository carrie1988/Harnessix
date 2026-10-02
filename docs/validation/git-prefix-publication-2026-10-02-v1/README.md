---
doc_type: validation-evidence
status: current
version: 4
code_revision: f887aae8bf54789fa2424f7cbc62bd335a1ccd47
owners: [core]
modules: [session]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/session/test_git_prefix_publication.py
  - tests/session/test_git_publication.py
  - tests/session/test_publication_seal.py
  - tests/agent/test_authenticated_store.py
  - tests/artifacts/test_authenticated_body.py
  - tests/product_config/test_product_state_backup.py
  - tests/product_config/test_product_state_restore.py
supersedes: []
---

# Git独立尾锚端口验证包

## 交付范围

本包对应[总体与详细设计](../../changes/m09-r4-git-prefix-publication.md)，交付三个生产文件与一个新测试文件的有限认证端口。默认产品没有消费装配；不是完整W1、全前缀、当前Root归属或执行权证明。

## 阅读入口

- [实际验证报告](REPORT.md)：当前原378＋尾锚152＝530通过，P1原反证/正式RED→GREEN与全部失败边界。
- [源码与结果摘要](SUMMARY.json)、[完整输入](INPUTS.json)、[原行为保持](INTEGRITY.json)。
- [原归档成员定位](ARCHIVE_REFERENCES.json)、[复验命令模板](COMMANDS.md)、[评审包](REVIEW_PACKET.md)。
- [图形校阅](DIAGRAM_REVIEW.md)：签发图补入最终实际Scope复核并重新渲染/视觉核验，另外三图9文件原样。
- [正式DocGate](DOCGATE.json)、[验证合同](VERIFICATION.json)、[当前包清单](MANIFEST.json)。

## 版本与公开边界

版本3闭环[P1 GP-PREFIX-PEER-01](P1_CLOSURE.json)：仅修改新签发端口及其测试，正式行为RED先行；原524PASS与独立9PASS/1FAIL原件保留。该历史记录中的“独立复核待完成”描述版本3捕获时的状态，不是当前结论。

版本4补充[独立闭环与集成记录](INTEGRATION.json)：在相同四项源码/测试SHA下，新的独立审查确认原负例10通过、关联530通过、共同关闭优先级2通过，限定范围无新增P0/P1/P2；集成树关联530通过，完整434个生产源码类型检查通过。源码外安装验证及其宿主失败另列，不与源码测试合并为全平台验收。claims与原codec两源不变，旧证据不改写；公开投影不是原始日志，其原archive/member/SHA256与当前公开投影SHA分列。公开资料无个人环境路径或真实凭据。

## 同Wheel源码外验证

在macOS ARM64使用同一新Wheel建立Python 3.12.7、3.13.8两个新环境，177个显式测试文件各取得**4971通过、57跳过、0失败、0错误**。全部475个源/包成员与当前产品字节一致，安装RECORD和实际模块/子进程导入均校验，无运行时源码fallback；依赖仍为原70项锁定版本。

初轮验证宿主漏复制已跟踪的`provider_reverification_chain`管理脚本，双环境各4项收集错误。补齐77项tracked scripts验证输入后重跑，同Wheel、生产代码、选择器和断言不改，原失败留在私有原归档。该失败属于验证宿主输入缺失，不解释为产品Wheel应包含管理脚本，也不把收集数代替通过数。此结果不替代Windows/Linux原生、完整Git交付、R3或商用门禁。
