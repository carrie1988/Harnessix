---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: 2cc097250435938746d3320043eeb116e07f4633
owners: [core]
modules: [session, product_config, documentation]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/session/test_authenticated_body_refs.py
  - tests/product_config/test_git_decision_link_sources.py
  - tests/product_config/test_git_decision_source_sdk.py
supersedes: []
---

# 原事件正文来源、决定声明映射与解码观察研究交付

## 1. 范围及结论

主线实现原Session同次原字节定位及Git私有三变体声明映射，不增加SQL/DDL/Key/网络或执行入口。
[总体方案](../../changes/m09-r4-git-approved-link.md)及[十四节详设](../../changes/m09-r4-git-decision-original-body-sources.md)
包含架构、四图、字段、调用链、伪代码、失败/恢复、测试与部署回退。
正式决定Source Proof、Writer、完整生命周期Reader、恢复屏障、B4/B7仍开放。

## 2. 主仓终结测试与版本边界

| 集合 | 结果 | 证据范围 |
|---|---|---|
| 最终Session原字节/历史/Seal/Store | 166 PASS，4.057秒 | 实际SQLite认证及原控制回归 |
| 最终映射/原完整语义/旧决定合同 | 329 PASS | 51映射、136原语义、142旧合同；不是认证Writer |
| 整体实际SDK approved消费 | 1 PASS，夹具113.204秒 | 发生于UUID比较前置整改之前；不是最终Mapper同候选SDK复验 |
| 文档治理定向集合 | 23 PASS | 不替代功能、完整SDK或CI |

前两组495唯一节点无FAIL/ERROR/SKIP；加23治理为518唯一节点，SDK早先版本单独列示。之前17及36项映射中间运行均包含于最终51，不重复累计。
真实SDK在原父控制内消费已批准证据，完整U、MAC、源、Review和末端复核仍执行；
GitDB total_changes0、完整行、原源及业务状态不变，仍返回decision_not_linked，不发布决定事件。
整项夹具113.204秒不是read_all时长，未单独测量该read，原60秒consumer/120秒Turn未提高。
SDK是本地受控Provider场景，不调用百炼，不代表R3真实模型编码质量。

SDK之后的唯一生产调整是定位比较前置exact UUID/int检查；9项foreign-equality负控及最终完整纯集合通过。
为避免重复重型运行没有再执行该SDK。两版本源码SHA及差分明确绑定，不声称最终同候选整链通过或可发布。

## 3. 来源、核心实现及安全限界

[`EventBodyRef`](../../../src/harnessix/session/event_body_refs.py)仅保存原Thread/Event/Sequence/原UTF-8正文SHA，
[`原事件读取`](../../../src/harnessix/session/sqlite_publication.py)在MAC和原身份验证之后累积，
[`原历史协调器`](../../../src/harnessix/session/sqlite_history.py)在完整前缀/Reducer/关闭/最终控制通过后才交付tuple。
禁止通过AgentEvent模型编码器补造原字节，内部累积器拒绝列表子类和复用非空列表。
窄夹具与旧主仓基线均8宿主检查点、13SQL/12SELECT/BEGIN1；不是全链计数或性能等价证明。

[`声明映射`](../../../src/harnessix/product_config/git_decision_link_sources.py)复用原完整历史/Route解释，
拒绝普通state伪标签、缺失/混合/错序定位、错误CP及坏前缀；三种完整模型context消费同一父控制。
原异常载体保留ValueError/TypeError/OSError/取消首中末原对象。定位UUID/int在相等比较前按确切类型核对，
不能让调用方__eq__成为隐式回调。
普通聚合及格式合法调用方摘要依然能形成纯声明；这是明确非认证边界，不能直接交给原Prefix签发器或Executor。
没有默认Reader接线、Writer发布、授权令牌、新审批或Git效果。

## 4. 独立审阅及失败保全

私有来源配方13纯项通过不等于实际MAC来源验收；主审整改其未传模型context和仅信任标签的问题，
改用原完整语义解释器，未创建第二状态机。最初三个正向预期仍取根事件而非原请求定位的夹具错误单独保留。
独立审阅复现1项P2：错误UUID类型通过自定义相等被接纳，非MAC绕过；最终新增9项Spy负控闭环。
独立复验确认原probe拒绝且foreign_eq=0；它使用隔离依赖验证实际函数控制流，不是MAC/Pydantic集成，
最终主仓9项Spy另覆盖实际调用。源绑定按原件记录，不将独立审阅未执行的SDK写为通过。

主仓missing-module RED、两个TurnStarted夹具错误、mapper语义/context RED及初始lint问题保留；
后继修正不弱化原断言、安全边界或容量。旧数据库、业务项目、整改参考、foreign untracked及封存原件不修改。

## 5. 响应性研究候选：不合入生产

独立原型仅在已验真不可变prepared正文decode处采用full Before→progress leaf→撤销→full After，
公开codec/未知回调/混合历史/U/实际IO/发布/terminal保持原full，原解析及512KiB不变。
28最终短测、2实际progress引用/SQL代际负控及1真实SDK候选read通过；read21.894秒、最大同loop间隔5.887秒、
total_changes0及全行hash不变。基线Revision不同，无同字节配对baseline，不计算提升比例。

已复现外callback次数/副作用、暂态Owner引用/物理ABA和首异常差异，不能宣称透明优化。
原IO-trap仅一个实际成功输入闭包；完整30矩阵、到期自然耗尽、审批等待余量和完整SDK均未完成。
生产合同HOLD、默认关闭、未合入主仓，P1/B4/B7不关闭。详见[成本研究更新](../../research/git-prepared-cost-attribution.md)。
原收集ERROR、6项夹具FAIL、旧Git启动FAIL及lint失败全部保留。

## 6. 预算、Docker与总体门禁

新60元周期26条completed，已知估算0.706096元、预留0、估算余量59.293904元，实际账单未知；
本交付付费请求0。旧两笔未决不纳入当前周期、不报零费用或已结算，新未决仍失败关闭。
Docker官方status退出1、engine socket缺失；桌面接口本次明确返回Mac锁定，未绕过锁屏/TCC或恢复原8容器。
默认Workspace挂载/Profile仍未验收。登录需求Console/Network披露面、实际整改、全功能回归和浏览器接受仍开放。
R3旧严格0/20及必需测试1/20、真实Beta接受0、三平台和R1～R6保持；本交付不形成1.0商用发布结论。

## 7. 交付物、安装与回退

[结构事实](facts.json)、[Review Packet](REVIEW_PACKET.md)、manifest与同次源码摘要绑定公开件；
私有交付另含全部初败/最终JUnit、原源码绑定、独立审阅、研究patch、图像及逐成员Manifest。
无DDL或安装依赖；回退同步撤销来源依赖与Session元数据，不删除历史、不重签、不扩大旧Reader接受集合。
旧先前公开报告的903节点措辞订正为原四JUnit去重924，原事实/失败不改；不能叠加到本次通过数。
