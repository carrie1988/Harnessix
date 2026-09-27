---
doc_type: adr
status: current
version: 1
code_revision: 7be9fa218ff6eef275f1b82d65ed36c066df34ba
owners:
  - core
modules:
  - trusted_actions
  - processes
  - artifacts
related_adrs:
  - docs/adr/0093-kernel-owned-public-failure-contract.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
related_tests:
  - tests/trusted_actions/test_success_projection_boundaries.py
  - tests/trusted_actions/test_output_budget.py
  - tests/trusted_actions/test_projection_lifecycle.py
  - tests/trusted_actions/test_process_success_projection.py
  - tests/trusted_actions/test_success_projection_runtime.py
supersedes: []
---

# ADR-0094：成功Owner投影绑定审计双摘要并在序列化前有界

## 状态与背景

接受；不代表发布门禁已关闭。完整接口、数据、时序、失败和测试见[详细设计](../changes/m09-4a-success-output-projection-boundary.md)。
成功Outcome只是动作效果事实，不授权Provider追加任意内容。前序版本14个负例证明成功投影跳过
验证，且JSON大小检查若位于序列化之后不能约束进一步分配与类型回调。

## 决策驱动因素

- 成功、失败、执行、恢复使用同一公开边界；不能把反馈失败改写成动作失败。
- 原生JSON先限制资源，再交给通用编码器和DTO，避免扩展对象方法成为隐式执行入口。
- 复用正式ArtifactRef和Process/Eval DTO，不创建重复运行时或独立服务。
- 不改旧Plan/Binding指纹，不用新增可配置输出授权掩盖迁移问题。

## 候选方案与取舍

| 方案 | 结果 |
|---|---|
| Provider身份受信即可公开 | 否决：真实外部数据仍可能被追加，身份不能授权任意诊断。 |
| 只做Hash或Secret正则 | 否决：Hash不证明Process业务语义；正则不证明引用和审计绑定。 |
| 全部成功正文删除 | 否决：破坏正式Process/Eval反馈和诊断工件。 |
| 事后JSON字节检查 | 否决：大树和扩展类型已进入编码器，不能形成序列化前边界。 |
| 内核共用合同、原生预检、独立时限 | 采用：保持效果事实，限制投影资源，失败关闭且无需部署单元增加。 |

## 决策

配置Owner的所有投影均验证原摘要Hash和正式ArtifactRef SHA；Process/Eval另外校验DTO、计划UUID、
profile和终态语义。成功Process只接受零退出，Eval非零测试退出仍是基础设施成功且passed=false。
局部不可变预算默认1MiB、64层、10256节点、10秒，整数bit_length≤128，模型不可控制。

预检使用精确原生类型、迭代栈、退出标记和进入前节点核对；编码后独立核对字节并复制JSON。
异步期限覆盖Provider和后置验证，取消由CancelToken托管回收；同步阶段设置单调时间检查点。
Owner自抛TimeoutError与本层真正超时分开。投影故障只影响结果公开，不改写Audit，也不重执行。

## 后果与适用边界

- 旧仅SHA引用测试夹具升级正式ArtifactRef；不能放宽生产合同迎合测试。
- Runtime故障终结可能查询Owner一次补偿，保存终态后resume不重复Provider；动作次数始终不增。
- 预算不是RSS等值保证，也不能硬中断无await阻塞或吞取消的Provider。
- Owner真实字节、所属域、内部预算继续归Owner/Artifact Store；执行器原始Outcome预算和无Provider
  成功JSON仍为独立发布缺口。本ADR不取代ADR0093，也不关闭整体0.9.4a。
