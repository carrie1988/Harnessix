---
doc_type: validation-evidence
status: current
version: 1
code_revision: 38cf8f7f601ce523d3c76b3473cf8d266339ed0d
owners: [core]
modules: [product_config, session, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_decided_source_reader.py
  - tests/product_config/test_git_decision_link_sources.py
  - tests/product_config/test_git_prepared_link_connection.py
supersedes: []
---

# 原资源决定读取与真实终端边界验证

## 1. 摘要与判定

原历史 Reader 新增完整只读 `read_decided`。主仓451唯一功能节点通过；返回普通决定事实，不产生 Git 决定记录或执行权。
**正式 Writer No-Go；B4/B7/P1 与 R1～R6 保持开放。** 本报告不宣布1.0商用发布。

## 2. 需求背景与输入

原事件正文定位及映射已经存在，但只有测试内在原读取过程中消费；新增真实原资源入口才能避免把调用方 Evidence 当认证。
输入基线为 `38cf8f7f601ce523d3c76b3473cf8d266339ed0d` 的 tracked 源码；外部未跟踪目录、客户原目录与参考整改件均不进入本交付。
源码/结果摘要见 [facts.json](facts.json)，总体方案及十四节详设见 [来源设计](../../changes/m09-r4-git-decision-original-body-sources.md)。

## 3. 设计目标与非目标

只读入口复用原完整 Git MAC、Session/Route/Execution/Core/CAS/Review/U 和原同步终端，不复制批准状态机。
不提供新授权、DDL、Key、配置、缓存、默认工具、事务 Writer、效果执行或恢复屏障；R3真实质量及Beta独立验收。

## 4. 总体架构与数据流程

原 Route UUID → 原父控制和60秒操作 → 全部 Git 前缀认证 → 全部原资源及完整 U → 同次选择目标 → 闭合声明 → 原同步末端 → 普通事实。
输入不含调用方 Evidence、hash、ApprovalRecord 或 Token。定位不是认证能力，返回值也不是跨操作发布凭据。
源码流、时序、接口、字段与伪代码在来源详设中逐项映射，避免另建第二读取系统。

## 5. 接口、契约与源码

- [原历史 Reader](../../../src/harnessix/product_config/git_prepared_approval_history.py#L167)：`read_decided(route_id, *, cancel, checkpoint)`。
- [私有映射](../../../src/harnessix/product_config/git_decision_link_sources.py)：原完整语义重放、原正文定位及三种声明。
- [原控制](../../../src/harnessix/product_config/git_prepared_link_ledger.py)：实际原资源、SQL代际、四库、终端读集合。
- [新接线测试](../../../tests/product_config/test_git_decided_source_reader.py)：19项替身控制短测，只证明接线和错误传播，不冒充实际 MAC 验证。

原构造器、read_all及所有原顶层辅助函数AST保持；生产增量为只读方法和私有末端返回绑定类（含依赖导入）；不改原旧方法/辅助AST。

## 6. 正常与失败控制

全部原关联先读，再定位目标；坏非目标关联不能被跳过。目标缺失/pending拒绝，不构造部分结果。
原父无await同步终端成功后才交付其严格快照；callback前构造的旧别名不交付。
终端和映射控制异常保持原实例；实际 timeout 到期与上游 TimeoutError 分开，不刷新期限。

## 7. 主仓回归

| 组 | 唯一节点 |
|---|---:|
| 新入口接线/异常 |19|
| 新返回末端绑定 |12|
| 原正文映射 |51|
| 决定数据与Wire |142|
| 原全语义投影 |136|
| 原连接来源与事务 |80|
| SQL窗口生命周期 |3|
| 原Session字节定位 |8|
| **合计** |**451**|

最终JUnit耗时以facts为准，0失败/错误/跳过；451按nodeid去重，不与前版495功能或本次独立81控制回归叠加。
Ruff/Mypy单列，不以静态检查代替业务或真实SDK。治理26项另计；中间438与28项包含于最终451，不叠加统计。

## 8. 实际 SDK 验证与返回绑定

最终主仓同源码SDK一次1PASS：完整夹具123.154秒、外层进程123.719秒，非一次consumer耗时；原60秒consumer/120秒Turn没有提高。
原实际资源批准回读成功，读写计数0；主动损坏唯一原MAC行后，用不存在的目标UUID仍先以publication_history_unproven拒绝，
不是忽略非目标坏链返回missing。Session/history与源未变化，故障注入的1次写单独记录。
首次隔离Git2.53候选SDK117.454秒为返回Guard之前版本；中间EF候选SDK120.418秒不替代最终交付快照版本。Apple Git初始bootstrap FAIL保留。
独立审阅复现最后外callback只改返回合法SHA的三变体失败；最终新增私有_DecidedReadSet，原父terminal后以internal-only原mapper重建完整来源，
对待返回对象原strict深快照，再完整比较，最后只交付实际核验的新快照，而非回调前的旧别名。19项接线及12项正常/合法SHA/外来比较负控通过；不新增外callback或await、不签发Token。
独立审阅确认第二个P2别名重定向问题已闭环，审阅仅使用最终源码AST分支探针，不冒充完整SDK或MAC验收。
该执行使用离线可控Provider夹具，不是百炼、真实编码质量、三平台安装或SLA证据。

## 9. 真实晚窗口反例

独立冻结基线上的三个真实拒绝要求仍FAIL：
1. 完整 U 的末轮history之后本地config改变；
2. 同一末轮的symbolic HEAD/Ref改变但目标OID不变；
3. 完整 U 结束后本地config改变，prepared同步terminal仍接受。

第三项110.823秒为完整夹具时长，不是单次consumer耗时。原Git只读写计数0、全行与尾锚不变，不能因此宣称Git配置一致。
所有失败保留，不添加查询后把最终窗口移动并认定B4关闭，不用四库监视替代Git原语。

## 10. FD与锁调用边界

真实原生SQLite A→B→A换回实验中，路径恢复A、exact连接却读取B；两个只读/读写分支均复现。
这证明cooperative路径pin不是FD来源认证；不说明默认产品已发生攻击，也不加入CTypes/私有SQLite指针依赖。
审批与sync恢复及_finish持原Runtime锁；普通执行、启动恢复与直接端口不是同一全程Thread锁窗口。
另一个Task持锁时 `locked()` 同样为True，不能作为当前Task的锁能力。
独立五次边界验证共85 PASS/3 FAIL，只是分项结果，非完整套件通过；81控制节点与主仓组有重叠。

## 11. 持久化、恢复与安全

新入口不BEGIN/COMMIT、不新增业务行或补签；原登记连接、显式原事务及完整认证保持。
正式Writer未启用，不能把No-Go缺口转成批准写入；完整恢复仍先原双账本协调。
本轮无API/Keychain/客户原目录或参考件操作，没有密码整改或新增Beta接受；原350项前测FAIL不改。

## 12. 费用、部署与环境

新60元周期保留26 completed、估算0.706096元、预留0、剩余估算59.293904元；本交付百炼请求0。
实际账单未确认，旧未决原件保留且不计新周期，不认定其结算为零。
Docker官方CLI不能取得运行状态，后续8秒有界观测超时，engine socket不存在；未修改设置或容器，不确认原8容器恢复。
没有新发行物、依赖、服务或平台支持声明。

## 13. 发布风险与下一步

正式决定事务持久化/完整Loader/恢复、真实Git效果与Commit仍未落地；B4一致性、B7 FD及实际dispatch、P1响应性需各自闭合。
三平台同候选、R3固定20Trial、独立Beta及R1～R6不因只读功能通过而退出。
协作写入还是强排他工作区合同尚待决；确认前保持旧门禁和Writer停用，不暗改保证范围。

## 14. 交付、审阅与证据

公共交付包含本报告、[Review Packet](REVIEW_PACKET.md)、结构化facts及摘要manifest。
私有报告保存原JUnit、失败原件、候选与fixture来源、精确命令、AST/格式收据、图渲染、独立审阅、发布提交证明；封存件不覆盖。边界报告Manifest有1686普通文件hash匹配及3个未绑定链接身份的夹具symlink，
该限制补充审计，不follow链接冒称1689种物理身份都已验证。核心源码/日志及真实反例未漂移。
只读入口完成与完整商用产品完成是两个判定，不根据节点数量报告虚假的整体百分比。
