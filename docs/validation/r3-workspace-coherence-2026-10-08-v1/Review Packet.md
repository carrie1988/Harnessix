---
doc_type: validation-evidence
status: current
version: 1
code_revision: b182cf658dba40930bc8c8e0dc2123183da18dbd
owners: [core]
modules: [sandbox, product_config, evals, documentation]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/integration/test_task_pack_profiles.py
supersedes: []
---

# Docker Workspace gRPC FUSE 复验评审包

## 1. 评审入口及输入身份

- [正式完整报告](README.md)、[结构化事实](facts.json)、[资料核验结果](qa/documentation-validation.json)。
- 固定源码：`b182cf658dba40930bc8c8e0dc2123183da18dbd`。
- 固定Pack：`harnessix-engineering`版本2，10个原Case与Profile，原镜像、命令、资源、审批和门槛保持。
- 受控证据逻辑标识：`r3-restored-engine-20261008-v1`及其`grpc-fuse-revalidation-v1`子集。
  具体列名成员见报告第3节；原件不公开复制。
- 后继真实Suite：[独立停止投影](real-suite-stop.json)，仅作分阶段补充，不改环境API0或工程成绩。
- Desktop版本4.94.0采用固定环境声明；Engine29.8.2和实际`fuse.grpcfuse`分别有列名运行证据。
  Desktop安装包版本没有在列名JSON中独立证明。

## 2. 建议接受的事实范围

| 子项 | 复核依据 | 可以接受的结论 |
|---|---|---|
| 后端实际生效 | `ACTUAL_FILE_SHARING_AFTER_APPLY.json` | 本次实际挂载为`fuse.grpcfuse`；不是仅UI选择 |
| 六条一致性观察 | 后继`MOUNT_COHERENCE_RESULT.json` | 默认/显式端点的initial/create/atomic_replace全部通过；替换inode改变 |
| 原有固定Profile合同 | 后继结果JSON与JUnit；[原测试](../../../tests/integration/test_task_pack_profiles.py) | 10/10用例通过，错误0、失败0、跳过0，exit0；测试进程19.064秒 |
| 旧失败保留 | 初次一致性结果、结果JSON与JUnit | 5条一致性观察4 PASS、1 FAIL；10项Profile 5 PASS、5 FAIL，不被后继通过取代 |
| 费用边界 | 列名请求计数及旧预算快照 | 本阶段新增真实模型/API请求0，60元共享账本没有重置 |

这里的接受是对本次有限工程复验事实的建议，不是已经完成独立评审或最终封存的声明。

## 3. 重点复核问题

- [ ] 实际挂载证据是否明确`fuse.grpcfuse`，且与UI应用、Engine可用证据分开？
- [ ] 是否保留初次第5条默认端点原子替换FAIL，并将第6条显式端点写为未记录，而非补记？
- [ ] 后继是否恰为6条，3阶段×2端点，inode改变且无预先目录枚举、等待或结果重试？
- [ ] 新旧JUnit是否逐项覆盖同一10个Case，无新增过滤或跳过，旧5项失败集合全部列明？
- [ ] 是否区分外层19.064秒与JUnit18.510秒，而不把两者相加或宣称性能改善？
- [ ] 是否明确`ScriptedProvider`和固定答案补丁，不将10/10解释为模型自主修复或20 Trial质量？
- [ ] direct/readdir/direct是否仅作为路径可见性关联线索，而非Docker供应商根因证明？
- [ ] 业务恢复是否只保留有限操作与网络状态，而不宣称全部业务容器未操作或应用验收通过？
- [ ] 本阶段请求0是否与旧累计26请求区分，旧60元账本及未确认账单状态是否保留？
- [ ] 后继是否如实保留5.617秒后停止、Case0、标准Trial报告0、质量字段null，而不造新0/20成绩？
- [ ] 后继Usage完整但Attempt失败的请求是否仍为账本unknown及20.77824元预留，不手动结算或改门槛？
- [ ] 是否不含原日志/私有路径/身份/凭据，后继独立分阶段，不变更Profile或发布门槛？
- [ ] 两幅图是否与当前源码、探针记录及文字对应，且由本地渲染器成功生成？
- [ ] 最终封存是否单独形成完整Manifest，且不以资料核验记录冒充最终封存？

## 4. 明确拒绝的结论推广

1. “R3模型质量通过”“模型能自主完成全部修复”“真实Beta或商用发布通过”。
2. “Docker供应商根因已证实”“所有VirtioFS环境必然失败”“gRPC FUSE永久修复全部问题”。
3. “业务网络恢复或容器running即业务验收通过”“整个过程没有业务容器操作”。
4. “Engine可用即挂载生效”“容器命令exit0即内容一致”“跳过也算Profile通过”。
5. “60元预算已经重置”“26是本阶段新增请求”“快照就是后继实时余额”“未产生新增请求即实际账单已结清”。
6. “后继PASS使旧FAIL失效”“未记录的显式原子替换已通过”“新旧计数可累计为20个不同Case”。

## 5. 验证命令及适用性

以下命令在本新增交付目录执行，只读取公开材料；不执行Docker、网络或模型调用。
`PYTHON`应为具备Python3.12及PyYAML的已安装本地环境。完整列名原件对账与单文档政策
检查结果保存在资料核验JSON中，不代表全库文档门禁或产品测试重跑。

```bash
"$PYTHON" -B -m json.tool facts.json >/dev/null
"$PYTHON" -B -m json.tool qa/documentation-validation.json >/dev/null
jq -e '.virtiofs_baseline.coherence.status == "FAIL" and
       .virtiofs_baseline.coherence.observations == 5 and
       .virtiofs_baseline.profiles.passed == 5 and
       .virtiofs_baseline.profiles.failures == 5 and
       .grpc_fuse_revalidation.coherence.observations == 6 and
       .grpc_fuse_revalidation.coherence.passed == 6 and
       .grpc_fuse_revalidation.profiles.passed == 10 and
       .grpc_fuse_revalidation.profiles.process_seconds == 19.064 and
       .stage_requests.real_model_api_requests == 0 and
       .budget_snapshot.ledger_reset == false' facts.json
jq -e '.status == "PASS" and .documentation_policy.findings == [] and
       .evidence_reconciliation.status == "PASS" and
       .diagram_validation.svg_count == 2 and
       .diagram_validation.png_count == 2 and
       .scope.docker_operations == 0 and .scope.network_requests == 0 and
       .scope.real_model_api_requests == 0 and
       .scope.final_manifest_created == false' qa/documentation-validation.json
git diff --check -- .
```

未跟踪新文件的行尾、UTF-8及换行完整性另由资料核验检查；不以`git diff --check`覆盖未跟踪文件的空结果证明完整。
真实工程复现使用报告第9节的独立源码、独立Fixture和原Profile测试命令；不能在资料核验期间自动执行。

## 6. 风险与封存责任

- 本阶段只提供有限工程子项证据，R3真实质量、业务/Beta接受和商业发布保持独立。
- Desktop升级、切换后端、回退或更换宿主时重新建立输入和结果版本，不能继承本次PASS。
- 后继真实模型阶段另记录2请求：首个completed，第二个协议失败且账本unknown。
  当前快照28请求、已知估算0.725956元、预留20.77824元、剩余估算38.495804元；原60元账本没有重置。
  具体结算事实见独立公开投影，不把Usage完整当作成功终态或已结清。
- 后继完成Case0、标准Trial报告0，不形成新0/20质量成绩。认证HistoryReader读取成功且Session摘要不变；
  初始`TextContent.type`诊断脚本错误保留，v2成功不是产品缺陷修复证据。
- 未知工具名原值没有持久化；不能认定原名、拼写或别名误用，也不能宣称Decoder整改完成。
- 业务最终原8个容器全部running、4个healthy，身份和持久配置摘要保持、重接2个原声明网络；业务验收未证明。
- 最终成员清单及摘要见[Manifest](manifest.json)，整合后的核验见[最终核验](qa/final-verification.json)。
  不对既有原件重新归类，不删除旧失败，不把资料静态PASS当作运行质量或发布许可。

## 7. 最终整合与冻结范围

资料撰写阶段的59项限定对账及图示检查与最终整合门禁分别记录，不将两者相加为模型或产品测试。
最终公开Manifest仅涵盖本目录，自身不递归哈希；私有原始证据另封存，不包含源码环境或认证Key。
