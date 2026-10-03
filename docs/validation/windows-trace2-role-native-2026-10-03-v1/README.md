---
doc_type: validation-evidence
status: current
version: 1
code_revision: 9a0d84aaba6243539abd63e2de69b482350486a2
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_cas_integration.py
supersedes: []
---

# Windows角色接线后固定原生结果

## 1. 结论

固定9a0d84a、Run37099316276、attempt1实际终态failure。
已实际执行，16件源码与固定PE/PDB预检通过，有限artifact上传成功；未超时、重跑或下载动态日志。
[原生Run](https://github.com/carrie1988/Harnessix/actions/runs/37099316276)及
[原有限结果](result.json)保持失败：两Case均Git128、Worker2、input proof ABSENT、SDK未通过、Root UNKNOWN。
不因离线767项或源码检查通过改变该终态。

## 2. 新证据与边界

角色接线前有限记录缺少ENTRY_START_MATCHED并报告STREAM_BINDING_MISMATCH。
新记录两Case均出现ENTRY_START_MATCHED、DISPATCH_HASH_OBJECT、REPO_EVENT_SEEN，
profile MATCHED、返回一致MATCHED_128，已知格式HASH_OBJECT_ADD_AGGREGATE；
但仍UNCLASSIFIED_FORMAT/UNKNOWN，不能升级为独立MAC、Root或业务效果证明。
这些有限观察支持诊断start预期绑定已经匹配，后继静态求证重点推进到对象插入错误与未分类格式；
不证明唯一根因、errno、stdin读取或对象库写入成功。CDB arm/branch完整标记均0，原生见证仍未建立。

## 3. 验证与资料

result.json完整SHA256为0a37c29cadb688c14c373102c1930f8a326b3e7d985cc02cb8bfa9b82aceebdf，
与[result摘要](result-sha256.json)一致。archive恰含这两个文件；Run实际SHA、attempt、终态和两Case形状已核对。
预算保持20/45/300/240秒。公开[结构化事实](facts.json)、[证明边界](verification.json)、
[评审范围](review-packet.json)与[清单](manifest.json)限定数据身份和覆盖范围。
实现及四图见[完整设计](../../changes/m09-r4-windows-trace2-role-input-binding.md)，
767项与类型差分审查见[离线资料](../windows-trace2-input-binding-2026-10-03-v1/README.md)。
未重复生成离线证明或改写旧Run，未读取raw CDB/stdout/stderr或任意动态fmt/msg。

## 4. 后继与发布门禁

后继先从固定官方Git源码求证error_errno/die_errno到Trace2 fmt的机制及对象插入调用链。
只有明确有限的来源才可进入版本化目录，不能开放动态输出、忽略未知或推断唯一errno。
任何新行为必须固定新源码身份和原批准，不能因失败自动重发同一Run。
Windows SDK和消费者平台、完整Git/Backup v2、真实R3、独立Beta及商用R1～R6继续开放。
新增模型请求0，费用账本及两项未知预留均未改变。
