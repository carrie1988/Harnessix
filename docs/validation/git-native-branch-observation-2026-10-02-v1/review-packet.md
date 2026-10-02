---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: dfba34e707ed845f3e9d844461e124015c22dca7
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
supersedes: []
---

# 原生返回分支观察候选评审材料

## 评审对象与结论边界

评审对象为[设计](../../changes/m09-r4-git-native-branch-observation.md)、新增workflow、新增脚本及新增治理测试的冻结候选字节；既有16件输入只读、不修改。输入集与本地验证见SOURCE及verification。当前状态为等待独立审查及合入授权，不是dispatch授权。

## 必须检查的门槛

1. 只有一个手动Windows Job，默认dryrun，无自动触发或重跑；显式执行固定40位提交。
2. metadata SHA先于网络；资源固定官方PDB ZIP，全SHA再解压，成员SHA再绑定原现场Git；不替换Git或安装SDK。
3. 两处真实RVA必须执行到返回检查，或根据FSTAT失败明确INDEX未进入；缺失/错误上下文/echo/ARM/helper成功不得替代。
4. 两个硬件执行断点，不改指令、寄存器或对象；只读边界寄存器及固定身份字节，无Dump/Secret/正文。
5. 原13hook、22argv、20秒/45秒/5分钟/240秒、降权RO+DOD、Owner/Job、MAC/双raw/EOF/protection、容量和原两selector不变。
6. 原业务失败必须继续非0；调试观察完整不等于产品验收；A/B顺序不伪称跨进程认证。
7. 公开artifact精确两文件，没有私有目录、原日志、对象或动态正文hash；历史FAIL及旧证据不可覆盖。

## 已知风险与下一允许动作

现场CDB加载、硬件断点、调试对象清理及其与原Windows Owner/Job的相容性尚未验证。本地合成结构、模板和mock测试不能替代这些原生事实。真实静态fstat路径不按快照路径重开，降权句柄已有READ_ATTRIBUTES，因此没有新增权限或取消保护的修复依据。

候选本地验证完成后仍须独立审查最终源码协议、固定提交与输入SHA，再单独授权手动原生执行。在此之前不dispatch、不push；生产实现不变。原生若不命中或身份不匹配，应保留不完整结果与UNKNOWN，不扩展泛化生产字段。
