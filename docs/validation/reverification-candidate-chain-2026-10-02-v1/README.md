---
doc_type: validation-evidence
status: current
version: 1
code_revision: 4a9264bfeb84f04fb4976894bf350091dd8870b0
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_reverification_chain.py
  - tests/evals/test_provider_reverification_rebinding.py
  - tests/evals/test_provider_reverification.py
  - tests/evals/test_provider_verification_budget.py
  - tests/evals/test_provider_verification_host.py
supersedes: []
---

# 同一复验额度的不可变候选链验证包

对应[总体与详细设计](../../changes/m09-r3-reverification-candidate-chain.md)。
本包只证明受控验证宿主追加管理合同，不证明真实编码质量或商业验收。
尚未登记实际预算文件，新增模型请求为零。

## 实际验证

关联验证组固定九项源码／测试输入，284通过、零失败／跳过，前后字节无漂移。
四项管理源码显式类型检查通过，五项变更文件格式及静态检查通过。
四份Mermaid图实际渲染和逐图视觉检查通过，图源与PNG相邻保留。
独立审查77项补测与原43项共120通过，限定范围无P0/P1/P2；实际结果及输入边界见[verification.json](verification.json)。
原开发22失败、早期22及281通过、类型宿主首次拒绝均保留，不相加为新测试总数。

## 交付清单

- [facts.json](facts.json)：九项精确输入、版本、金额及零实际账本／模型操作事实。
- [verification.json](verification.json)：固定组实际结果、原失败及当前未验收边界。
- [review-packet.json](review-packet.json)：原安全规则、验收范围及独立复核状态。
- [manifest.json](manifest.json)：完整本包、设计和源码摘要；不宣称全仓快照。
- [架构](architecture.png)、[流程](binding-flow.png)、[时序](registration-sequence.png)、[数据流](cost-data-flow.png)：实际图及相邻mmd。

## 安全与失败语义

原70元周期及单个40元累计授权不变，旧20.77824元未决预留不释放。
追加不覆盖V1首次绑定或任何请求前缀，旧Suite不重新获得请求资格。
新增未决阻断新请求、重开及后继追加；默认未指定复验身份仍被旧未决阻断。
fsync失败保留可能已发布的实际结果，幂等确认不覆盖旧链、不重发模型请求。

## 复核方式

在固定源码下执行：

```bash
uv run pytest tests/evals/test_provider_reverification_chain.py \
  tests/evals/test_provider_reverification_rebinding.py \
  tests/evals/test_provider_reverification.py \
  tests/evals/test_provider_verification_budget.py \
  tests/evals/test_provider_verification_host.py
```

上述测试只使用临时私有账本及离线响应；禁止替换为实际预算路径或放宽原断言。
管理合同通过也不表示后继Suite已登记、已开跑、费用已产生或达到原20 Trial门槛。
