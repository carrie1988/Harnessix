---
doc_type: validation-evidence
status: current
version: 1
code_revision: 02a4c88a8dbac2b44c0ab0bc3b2733c2eb3dc675
owners: [core]
modules: [processes, tools, product_config]
related_adrs:
  - docs/adr/0097-typed-binary-publication-and-owner-protection.md
  - docs/adr/0043-git-and-controlled-test-feedback.md
related_tests:
  - tests/processes/test_raw_output_receipt.py
  - tests/processes/test_raw_receipt_supervision.py
  - tests/processes/test_git_raw_observation.py
  - tests/processes/test_windows_raw_receipt.py
  - tests/tools/test_git_delivery_reader.py
  - tests/tools/test_windows_git.py
  - tests/product_config/test_git_baseline.py
  - tests/product_config/test_product_state_backup.py
  - tests/product_config/test_product_patch_rollback.py
supersedes: []
---

# Windows Git 原始认证与安全输出分离验证

## 1. 结论与适用范围

受测源码提交为`02a4c88a8dbac2b44c0ab0bc3b2733c2eb3dc675`，运行时输入与该提交逐字节一致。
本验证覆盖原始计量、Owner v2认证、Supervisor终态重验、正式Git基准消费及双版本完整状态备份。
完整[总体与详细设计](../../changes/m09-r4-authenticated-raw-git-observation.md)定义类、接口、字段、
架构图、流程图、时序、数据流、核心伪代码、失败语义及兼容升级边界。

**本机合同和源码外安装验证通过不等于原生Windows验收，更不等于R4或商用1.0通过。**
旧原生失败、初稿文档门禁失败、结构治理失败、结构抽取后测试夹具失败、未固定工具链的治理失败
和离线安装依赖缺失均保留。
各阶段选择器和源码摘要独立绑定，不以较早通过替代最终候选；集合重叠和双Python运行数不相加。
本变更没有真实模型请求、百炼凭据读取、费用账本操作或R3质量成绩更新。

## 2. 实现与安全边界

1. [`CapturedProcessOutput`](../../../src/harnessix/processes/owner_output.py)在脱敏前增量计算raw数量/SHA，
   只增加计数与哈希状态，不新增raw正文文件。
2. [`Owner回执`](../../../src/harnessix/processes/owner_receipt.py)以原单一MAC认证v2双流原始统计。
   v1字节、原Schema及黄金MAC保持兼容；缺字段、篡改、旧Reader和降级均严格拒绝。
3. [`Windows观察支持`](../../../src/harnessix/processes/windows_owner_observation.py)只为真实pipe输出FD
   设置CRT二进制模式；ConPTY保持v1。Owner分别约束原始与安全发布量，不提高数值预算。
4. [`Supervisor`](../../../src/harnessix/processes/supervisor.py)持有原锁，
   经[`原回执投影`](../../../src/harnessix/processes/receipt_projection.py)重验MAC、原身份、终态事实和序号。
   原Store CAS、状态提交、取消和控制关闭仍由Supervisor执行，不重放命令。
5. [`Git私有结果`](../../../src/harnessix/processes/git_observation.py)分离安全正文与raw；
   [`正式基准`](../../../src/harnessix/product_config/git_baseline.py)消费blob及漂移原始摘要，
   需要解析的元数据必须完整且与raw等长同SHA。脱敏统计不能冒充原始blob证明。
6. [`备份验证器`](../../../src/harnessix/product_config/state_backup_records.py)统一解析v1/v2，
   保持原MAC，物理文件继续核验脱敏持久前缀；完整backup/verify/restore不重签或重放。

## 3. 原生失败与共享LF夹具

前序主线CI `36717195047`的Windows Job `109892914822`实际失败：第一步45 failed、80 passed、2 skipped，
首个异常是在共享Patch准备后缺少审批请求，不能把此Job记为原生成功。
原夹具以`write_text`准备文件，声明的`expected_sha256`却绑定LF字节；Windows默认文本输出会转换换行。
本机显式LF/CRLF正负控分别得到一个审批请求和零审批请求，CRLF场景保留原文件、不执行Patch；
该负控不是Windows原生实测。

[`共享夹具`](../../../tests/product_config/test_product_patch_rollback.py)仅将两份声明LF的初始文件
改为精确`write_bytes`，新增LF正控与CRLF拒绝，审批前记录固定状态/错误码，不回显内容或路径。
原Patch摘要、事务执行器、审批合同、Rollback负载和断言不放宽。新原生Job必须独立确认结果。

## 4. 分层验证与统计

正式统计见[测试结果](test-results.json)及[验证判定](verification.json)。
基础初始专项94通过；扩大初始基础171通过/7原生跳过，包含同一94项。
结构调整之前的受影响关联回归在Python3.12.7与3.13.8各921通过/34跳过；不冒充调整后最终源码。
结构调整后的指定合同、真实Git、正式来源及Rollback回归在两版本各336通过，无跳过。
最终冻结源码和测试的受影响完整关联回归945通过/53平台跳过，运行前后全部输入字节一致。
独立安装Wheel的指定回归313通过；450个包成员与源码逐字节一致，包含全部409个Python模块。
这些集合重叠，不能将上述数字相加作为独立场景数。

新原生测试覆盖CRT的LF/CRLF/Ctrl-Z/非UTF8、跨认证块秘密替换、raw限额、受保护大blob、
可解析元数据改写拒绝、旧v1拒绝以及原取消/期限与子进程退出。
macOS只能检查收集、静态合同及平台跳过；真实Win32/Job/NTFS结果由同候选原生CI证明。
Windows Server Runner通过仍不能代替Windows11消费者安装升级或独立Beta。

## 5. 结构与机器合同治理

原热点长度和包依赖策略不放宽。Lease字段投影、二进制观察支持和普通Git固定命令调度从长类分离，
新回执状态校验按运行/终态拆分。最终静态报告按实际源码刷新，不修改治理阈值或原批准上限。
治理测试固定Git2.53后302通过；未固定工具链的前一阶段301通过/1失败保留，环境元数据不完整。
Mypy覆盖409源码文件；Schema生成检查保持旧v1字节并新增独立私有v2版本。
六幅设计图均需实际渲染且检查可读性；图形证据摘要与原件收录在交付索引中。

## 6. 原件、完整性与复验

私有原件的逻辑定位为`Harnessix/verification/windows-raw-receipt-20260930-v1`，
目录0700、原件0600，不进入Git；公开仅保留必要事实、摘要和验证范围。
[源码绑定](source-bindings.json)记录实际受测输入，[事实记录](facts.json)保存版本和原件索引，
[Review Packet](review-packet.json)逐项区分通过与待验证要求，[Manifest](manifest.json)核验公开包成员。
原失败文件不可被后继通过覆盖；日志/机器记录不包含raw正文或真实认证密钥。

源码外验证使用实际构建的`1.0.0rc1` Wheel，不从editable源目录导入；版本号不是商用验收证明。
首次包含全部开发依赖的离线安装因缓存缺少`librt`失败；正式产品依赖和测试依赖的离线安装成功，
不隐式联网下载，不把第一次失败删除或标成成功。

## 7. 升级、回退与未关闭项

生产者、双版本Reader、Git私有消费及备份校验必须同步升级。
旧v1软件不能读取含v2的状态；回退需使用升级前验证兼容的完整备份，不删除raw字段、改版本或补签。
恢复操作只验证和恢复原字节，不启动旧命令，详见[恢复手册](../../operations/recovery.md)。

仍开放：新候选原生Windows结果、Windows11消费者验证、Git业务状态闭合和完整产品Commit/Checkpoint，
后续Diff与新批准/写入恢复、R3完整真实20 Trial、独立Beta及最终R1～R6发布验收。
本包只证明明确覆盖的读取/认证/备份合同，不宣布这些条件完成。
