---
doc_type: validation-evidence
status: current
version: 1
code_revision: 37a1f01bee0dc4747af8680b4918e4c85cae266c
owners: [core]
modules: [delivery, workspace]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_object_references.py
  - tests/delivery/test_git_tree_closure.py
  - tests/delivery/test_git_material_cas.py
  - tests/delivery/test_cas_write_authority.py
  - tests/product_config/test_git_baseline.py
  - tests/product_config/test_git_delivery_source.py
  - tests/product_config/test_git_delivery_source_sdk.py
supersedes: []
---

# Git直接引用与完整普通文件树验真验证包

## 1. 结论与范围

本包对应[总体与详细设计](../../changes/m09-r4-git-tree-closure.md)，覆盖完整原始 tree／commit
直接引用解析、原 CAS 普通文件树验真、严格路径及显式预算、只读和取消边界。
研究基线不是新增实现提交；实现由1241件完整代码／测试／脚本／治理及配置输入绑定，
公开 [Facts](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)
与 [Manifest](manifest.json)分别记录内容、实测、审查及资产字节。

这是完整 Git 产品交付的只读材料验真基础，不是业务认证目录、默认 Commit／Checkpoint、
全状态备份或商用发布验收。产品容量及历史归档承诺没有因内部显式参数而被默认冻结。

## 2. 已实现合同

- 三种原对象材料复用原类型／格式／OID／长度验证；tree 解析保留 basename 原字节及20／32字节子 OID。
- 规范目录排序使用尾斜线；重复 basename、截断、零引用及非规范 mode 拒绝。
- commit 只提取首行 tree 和连续有序 parent；未知扩展／续行及消息留在原材料，不自动读取祖先。
- 原 CAS 完整回读根、全部子树及全部普通文件；未修改成员、空文件、二进制和可执行模式不被省略。
- 对象正文去重与路径展开分别计数；重复子树在每个路径均完整展开，不能以 OID 去重绕过条目预算。
- 四项预算无产品默认值；路径复用原 Workspace 规则，链接／gitlink、冲突、缺失、损坏及超限整体拒绝。
- checkpoint 原样传播取消／期限异常；不写 CAS、数据库、Git对象、工作树或Ref，不返回部分成功。

## 3. 同一输入的实际结果

| 范围 | 总数 | 通过 | 失败／错误 | 跳过 | 秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 新直接引用与完整树焦点 | 474 | 474 | 0 | 0 | 1.256 |
| Delivery／Process／材料／备份恢复／重启关联 | 1707 | 1649 | 0 | 58 | 193.763 |
| 原Git基准与产品来源／SDK关联 | 99 | 99 | 0 | 0 | 37.658 |
| 同一Wheel源码外Python3.12 | 972 | 970 | 0 | 2 | 104.977 |
| 同一Wheel源码外Python3.13 | 972 | 970 | 0 | 2 | 85.933 |
| 最终CI配置完整治理 | 302 | 302 | 0 | 0 | 25.000 |

两个源码关联组逐 nodeid 核对无交集，合并覆盖1806项、1748通过／58跳过，
包含前序1332关联案例的全部文件及新增474案例。其他集合有交叠，不相加。
单机 skip 不是Windows原生通过，源码外两个Python结果也不是两个消费者OS。
最终治理、静态、Secret、文档及输入复读结果以 Verification 为准。
最终Ruff／Format、423模块Mypy、原可读性和Schema检查通过；仓库及实际Wheel
3930项输入Secret扫描零命中，440份文档检查零问题。治理和静态检查不是实际编码质量成绩。

功能回归与打包时完整输入目录 SHA256：`9da4e8aade250f1fe99ec87414db92bde442155d6f101a69a9f9aa2c6a31eeba`。
最终提交输入目录 SHA256：`acc80cd4850883f291d733d2196f3ffcf698eb6a4d6608e97bc0d9a93b76c948`。
两目录均为1241件，仅CI诊断配置及其治理断言两项不同；全部生产源码、功能测试、构建与依赖字节不变。
最终CI配置重跑完整治理，原功能与源码外结果按实际输入绑定，不伪造它们已经在新CI配置下重跑。
唯一Wheel SHA256：`2197aec571ae765107828a59a798c11d84e194bc8b6665b53079f077c4d7571f`。
469个发行物成员中的464个包成员、423个Python模块均核对源码及两次实际安装字节，
RECORD同时校验；import审计确认实际来自隔离安装，而不是仓库src或Editable。

## 4. 开发校验失败及修正依据

1. 两项 tree 测试把第一分隔空格之后的合法前导空格文件名误当作 mode 非法。
   真实Git差分确认其合法；保留原FAIL，改为明确正控，不收紧生产路径能力。
2. 四项只读测试在快照之后首次执行SQLite元数据查询，该夹具查询建立WAL／SHM。
   初次查询移至验真观察区间前；仍保留全部文件字节／inode／mtime、行数据及total_changes核对。
   这不是生产CAS缺陷修复，不声称SQLite只读连接初始化没有元数据副作用。
3. 可读性报告携带可选revision元数据，与原无参数精确检查输入不一致。
   恢复规范生成方式，600／100／20和既有热点规则不变；原失败保留。
4. 初次文档检查发现五项规范语义标题缺失；完善需求背景、设计目标、接口／数据结构及可观测错误章节，
   没有更改文档策略。原日志及后继实测分别绑定。
5. 首次完整治理301通过／1失败：追加到run的诊断参数与原精确命令断言冲突。
   参数改放当前步骤的`PYTEST_ADDOPTS`，原run、全部选择器及3分钟不变；原治理函数另加env精确断言。
   不通过删除精确命令断言、忽略额外参数或延长期限处理该失败，原日志保留。

## 5. 原生基线与当前风险

固定37a1f01的[CI 36800438931](https://github.com/carrie1988/Harnessix/actions/runs/36800438931)
终态失败：文档、Container及macOS成功；Python3.12／3.13在原许可证检查失败；
Windows首步骤在119通过／2跳过后达到原3分钟上限，后继Git步骤未执行。
原日志不能唯一证明正在阻塞的案例或实体；当前仅新增逐项名称、60秒栈及耗时诊断，
保留全部原选择器和3分钟限制，不将观测增强称为根因修复。

新候选原生结果须独立取得；不能继承基线绿色步骤或把跳过解释为通过。
消费者Windows11、完整产品Git交付、真实编码质量、独立Beta和最终同候选发布仍未验收。

## 6. 审查、图示与剩余任务

独立生产代码审查核对两个新模块实际SHA，未发现P0／P1／P2；
审查者未运行函数测试，不能作为独立OS验收或1.0审批。
五份Mermaid图均实际渲染为PNG并逐张视觉检查；图示证据只证明图示语法和可读性。

后继必须完成认证对象目录／角色、GitDB全前缀、完整目标树和Diff、独立批准、
双工作树及新派生事务、默认Checkpoint／Commit、Backup v2和新根重新授权。
本包模型请求为0，未读取百炼凭据、不改真实费用账本、不生成R3新成绩。
