---
doc_type: adr
status: current
version: 1
code_revision: 0245d117adc7c385a4e42de4e023fd0d22bbb1cd
owners:
  - core
modules:
  - evals
related_adrs:
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0083-built-in-immutable-coding-eval-task-pack.md
  - docs/adr/0084-recoverable-sequential-eval-suite-runner.md
related_tests:
  - tests/evals/test_engineering_task_pack.py
  - tests/integration/test_task_pack_profiles.py
supersedes: []
---

# ADR-0085：采用版本化第三方派生Eval数据集与隔离黄金答案

## 状态

接受。

## 背景

0.9.2a～c已经建立Suite、Task Pack和可恢复顺序Runner，但种子Pack只有两个自研仓库、两个Case和两种任务类别，
不能证明真实Coding Agent在多仓库、多任务形态上的质量。直接打包完整上游仓库会引入大量依赖、脚本和供应链面；只
增加十个合成函数又无法形成有说服力的工程基线。Review任务还需要把Finding绑定到源码，而不能只信任清单中的行号。

## 决策驱动因素

1. 至少3个固定仓库、10个Case、五类任务各2个；
2. 无网络、无第三方包、固定Container即可重跑；
3. 来源、许可证、版权和派生关系可审计；
4. Review不调用LLM Judge，证据可确定重算；
5. 黄金答案不得进入Agent可见资源；
6. 数据集扩展不得引入第二套Agent、Provider、审批或工具运行时。

## 候选方案

| 方案 | 满足的驱动因素 | 不满足的驱动因素 | 成本/风险 |
|---|---|---|---|
| A. 完整克隆上游仓库 | 真实性较高 | 离线体积、依赖和供应链边界 | 高维护、高攻击面 |
| B. 全部自研合成仓库 | 简单、权利清晰 | 外部工程代表性较弱 | 容易过拟合Harnessix |
| C. 固定Revision的最小MIT派生夹具 | 权利链、离线、类别和可控复杂度 | 不代表大型仓库 | 需维护来源与生成门禁 |
| D. 运行时下载任意仓库 | 扩展快 | 不可复现、不可审计、网络风险 | 不接受 |

## 决策

采用方案C，并固定以下不变量：

1. `harnessix-engineering/v1`只包含三个明确Revision的MIT派生Benchmark Archive；
2. Manifest必须冻结上游永久链接、完整LICENSE摘要、Archive、Git Commit、Tree和Profile；
3. 十个Case按Bug Fix、Feature、Refactor、Test、Review各两个均衡分布；
4. Review物化时校验精确源码行字节摘要，失败关闭；
5. 黄金补丁只能位于Wheel外的开发Benchmark目录，只用于先失败后通过验收；
6. 正式Case Adapter复用唯一Agent/Session/Trusted Action/Campaign链，禁止另建Eval Agent Runtime。

## 理由

最小派生夹具保留来源与工程问题形态，同时把执行依赖限制为Python/Node标准库，使固定Digest Container、禁网和资源限制
可以形成稳定基线。Case专用Profile隔离同一仓库内其他故意缺陷；源码证据摘要使Review Oracle不再只是未经验证的元数据。
黄金补丁位于运行包外，可证明检查可解而不向模型泄漏答案。

## 后果

### 正面后果

- 任务类别和仓库数量达到0.9.2d数据规模门槛；
- Archive和Manifest可由源树确定重建，手工改哈希会被门禁发现；
- 第三方许可证随Archive分发，并有仓库级通知；
- Review Finding可定位到固定源码字节。

### 负面后果与债务

- 夹具规模较小，不能外推为大型单体仓库质量；
- Refactor检查包含结构Oracle，需要随任务版本维护；
- d1完成不等于离线Suite完成；d2/d3仍需真实Agent和每Case两次Trial。

## 兼容、安全与运维影响

新增Pack ID，不改变默认`harnessix-seed/v1`或既有Schema。Wheel新增三个Archive，不包含`benchmarks/.../solutions`。
运行时不访问网络、不读取上游工作副本、不接收任意路径或命令。许可证和来源进入`THIRD_PARTY_NOTICES.md`，0.9.4再由
SBOM/许可证门禁复核。

## 验证方式

- 生成器`--check`逐字节比较Manifest和Archive；
- 单元测试验证3仓、10 Case、五类各2个及全部物化；
- 每个Case执行Baseline失败、应用外置黄金补丁后通过；
- 篡改Review源码证据返回稳定错误；
- Container CI经现有Product Runtime、Approval、Trusted Action和Artifact执行全部Profile。

## 关联资料

| 类型 | 路径/链接 | 关系 |
|---|---|---|
| 源码研究 | [多仓库离线数据集研究](../research/multi-repository-eval-dataset.md) | 来源、许可证和任务证据 |
| 重大变更设计 | [0.9.2d详细设计](../changes/m09-2d-multi-repository-offline-baseline.md) | 实施增量 |
| 现行模块设计 | [Evals模块](../modules/evals.md) | 当前事实源 |
| 测试/验证 | [数据集测试](../../tests/evals/test_engineering_task_pack.py) | 数据、Oracle和黄金闭环 |

## 被取代关系

无。数据集增加任务时发布新的Pack版本或新的ADR，不改写v1身份。
