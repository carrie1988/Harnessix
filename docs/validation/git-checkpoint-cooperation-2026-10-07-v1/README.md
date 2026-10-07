---
doc_type: validation-evidence
status: current
version: 1
code_revision: c65d6dbef1032b3456c34359959f803aa063ec29
owners: [core]
modules: [product_config, delivery, workspace, agent]
related_adrs:
  - docs/adr/0007-agent-loop-and-cancellation.md
related_tests:
  - tests/product_config/test_git_prepared_link_controls.py
  - tests/delivery/test_terminal_read_control.py
supersedes: []
---

# Git 协作调度隔离研究验证摘要

判定：`CAS_BRIDGE_MECHANISM_VERIFIED_RESPONSE_P1_OPEN`；[完整研究](../../research/git-checkpoint-cooperation.md)描述源码、接口、流程、时序、持久化、失败与准入边界，[结构化结果](result.json)固定实际上下文。

## 1. 实际范围

最终机制25项与原终端/SQL限定集合16项通过。两次源码级SDK运行的是同一物理替换负控，不累计为两个质量场景。
原型缺陷RED为25节点、6失败/19通过；失败、修补前脚本与环境/夹具原错误均保留。

## 2. 未通过的发布条件

实际研究仍有20.419秒最大事件循环间隔；行认证/解码最长7.424秒、终端复核3.878秒，最大间隔尚未完整归因。
研究未改SQL C回调或同步终端，不缓存认证、不减少检查频率、不提高期限或容量。
主产品没有新依赖或调度桥，研究外部桥不在原认证配方摘要内，未取得新Wheel或三平台同候选验收。
R3冻结严格0/20、真实Beta0任务、模型请求0；两笔费用未决和R1～R6商用门槛保持。

## 3. 输入与复核

源码基线 `c65d6dbef1032b3456c34359959f803aa063ec29`，隔离克隆两处调用接线。完整实验包 `git-checkpoint-cooperation-20261007-v1` 包含来源全集、全部版本脚本、XML、日志、图像、Review Packet与Manifest。
最终机制字节不冒充r4/r5 SDK输入；混合源码/原Owner安装装配明确记录。当前仅允许继续研究，不能默认启用。
