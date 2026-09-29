---
doc_type: validation-evidence
status: historical
version: 2
code_revision: 850c7ba90bab5b1821015f3182c6ba6ac253e8aa
owners: [core]
modules: [models, agent, product_config, processes]
related_adrs:
  - docs/adr/0016-model-attempt-ledger.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/models/test_chat_terminal_diagnostics.py
  - tests/models/test_attempt_usage.py
  - tests/product_config/test_state_fixture_readiness.py
  - tests/product_config/test_product_state_backup.py
  - tests/product_config/test_product_state_restore.py
supersedes: []
---

# Chat终态安全诊断与产品状态夹具验证

## 1. 结论与交付边界

正式Chat协议、工具调用组完整校验、Usage、code/category/retryable、自动重试和Schema保持不变。
只有已确认内部类型的Chat终态失败，才在原Attempt错误消息中附封闭原因；Turn仍保留原通用错误。
未知异常与旧事件不补推原因，不保存原正文、ID、参数或异常文本。
本专项不证明真实模型质量改善，不重构未知费用结算，不发布商用1.0。

[Chat总体与详设](../../changes/m09-r3-chat-terminal-diagnostics.md)给出组件、三个图、接口、字段、
伪代码、持久化、恢复、错误与安全边界；
[产品夹具观测设计](../../changes/m09-r1-single-child-reaper.md#9-原生结果与产品测试观测边界)
说明公开状态等待与后台Task所有权，不使用关闭操作作为完成同步原语。
实际源码SHA、测试和工具链原件摘要见[Facts](facts.json)，
范围判定见[Verification](verification.json)、[Review Packet](review-packet.json)和[Manifest](manifest.json)。
头部Revision是原实现/设计基线；后继实际文件身份以Facts为准。

## 2. 正式失败、修复与恢复

固定原实现SDK RED为21项中19失败、2通过。新33项覆盖15种非法终态、4条实际SQLite Session
重开/回放链和类型安全正反例；所有失败均禁止释放工具，不重发HTTP。
关联1377项为1376通过、1跳过；焦点39项与关联重叠，不相加。
首次关联的两项Attempt/Turn消息全等失败保留；后继准确断言新消息，其余所有错误字段全等。
Anthropic、未知/早期失败、成功和max_output_tokens仍遵守原行为。

未知字符串、错误子类、原因被改写、非协议错误或非失败Attempt不生成详细原因；
闭合Enum不能由供应商文本替代。Secret Canary验证公开事件与日志不包含身份及参数值。
新消息沿原事件提交和认证Session回放，无新Schema、数据库、依赖、遥测或用户安装步骤。
原SDK/HTTP资源关闭与取消回归保持。

## 3. Windows原生结果与观察者取消耦合

固定`850c7ba`的[原生运行](https://github.com/carrie1988/Harnessix/actions/runs/36634123881)
Git/工具/四取消模式焦点138通过、5跳过；NTFS/默认审批61通过；产品重启10通过。
同Job完整备份/恢复109通过、1项SETUP错误，因此完整Job失败，不是Windows整体GO。
低敏探针旧Rename得到PermissionError、errno13、winerror32；新完整PID关闭/独立标记通过。
这是当前探针的正式分享冲突事实，不回推旧候选未保存的stderr内容。

失败发生在公共产品夹具五秒观察超时，直接gather私有后台任务，将取消传入Model History的SQLite commit。
恢复用例尚未执行，不将此错误说成已证实的恢复算法失败或磁盘死锁。
两项控制实验直接调用同一真实夹具，分别暂停实际事务commit await和Provider stream超过五秒，
原夹具两项均RED、后台任务确实被取消。修正复用已有公开SDK状态等待，不只是把原gather期限增大。
只接受正式completed；提前failed/cancelled/interrupted立即拒绝，所有备份、Key、Artifact、恢复断言保留。
修正后的120项产品状态/恢复/管理Root关联全部通过；该本机结果不替代后继原生Windows复验。

原实现旧CI确认仍在运行的WindowsJob仅记录权威状态，不因观察时间到达就重新启动。
本专项不承接旧候选的通过结果作为新源码同候选验收。

## 4. Docker网络、挂载与验证环境

默认Desktop CLI已成功拉取原固定Python RepoDigest，原八个容器保持运行；未更换镜像、评分器或Pack。
默认路径的新只读Workspace运行仍停在Created，观察期间未启动。该探针的容器已单独移除，
仅停止自有CLI观察进程，未重启Docker或触碰八个业务容器。
镜像下载成功不证明默认挂载或业务环境全部可用；先前同Engine显式端点GO保持其原限定范围。
此次零真实模型请求，原真实Suite部分FAIL和70元周期的未知预留未作新结算。

## 5. 验证方法、失败保留与发布判断

最终全量回归5994项：5886通过、108平台/能力跳过，零失败、零错误；耗时531.565秒。
专项分组已包含在该全量中；完整治理另取297项独立复验通过，不累加。

完整验证在不含未归属本地测试的独立工作树中执行；不读取、收集、打包或运行主检出的未归属目录。
首轮治理记录缺失的在建资料目标与系统Apple Git不使用测试自有配置的两个失败，原件保留。
资料完成后使用已固定Git2.53独立回归，不改全局配置、原断言或治理门槛。
初次图渲染因Puppeteer缓存缺少指定Chrome全部失败；复用本机已安装Chrome后，
实际变化资料中的15幅Mermaid成功生成SVG。全库867幅仅结构计数，不能声明全库已渲染。

发行物为内部1.0.0rc1 Wheel，三份Model生产文件与实际受测字节逐一一致；安全扫描包括仓库及实际Wheel。
全量回归、静态门禁和最终公开文件哈希以Facts最终原件为准，重叠分组不累加。

**只关闭本地诊断/观察者取消耦合专项；商用1.0保持No-Go。**
未知费用核对、完整20 Trial真实质量、默认Desktop Workspace路径、消费者Windows11、独立Beta、
发行权利及最终同候选R1～R6仍开放。不得把成功的安装、Mock SDK、单平台测试或内部RC称为商用验收。

## 6. 后续原生CI跟踪与安装操作修正

固定`65d7323`Windows Job仍为失败：原112项步骤先完成79项，包含两个观察者控制实验及原未决Manifest恢复，
随后被五分钟CI进程期限截断。旧109通过/1 SETUP错误与本次79通过/截断均保留，不能拼接为完整新PASS。
[后续原生事实](native-followup.json)绑定同一候选日志、实际Node ID集合比较和编排回归。
后继将原112项分为37/75两组，每步仍为五分钟；生产源码、恢复断言、业务期限、Schema与预算不变。
新工作流的完整原生结果需独立取得。

[当前安装操作](../../operations/installation.md)修正内部RC版本、退役Extra及尚不存在的PyPI安装命令，
直接复用原规范工作流的锁定依赖/实际Wheel哈希与源码外venv步骤。原0.1.0固定历史记录保持。
实际命令与全新解释器验证属于内部候选安装，不等于真实编码、消费者Windows11、Beta或商用发布。

本次完整治理299项通过、零跳过/失败/错误，耗时26.736秒；17项焦点已包含其中，不累加。
两项新增治理回归均先保留旧编排/旧安装操作的RED，再核验正式修正。
从安装手册直接提取公共构建和POSIX命令，在全新源码外venv实际安装内部RC；
锁定依赖与Wheel均使用`--require-hashes --no-deps`，隔离解释器导入434个包成员与实际Wheel逐字节一致。
Help、版本和License完成，不调用Provider；PowerShell新命令未在本轮原生Shell执行，不继承旧安装生命周期结论。

本轮仅改变编排、治理回归及资料，未更改生产源码、产品预算、依赖锁或原112项用例源码。
零模型请求、原未知费用预留不变；先前5994项全量保持其原候选记录，不改称新工作流全量结果。

同一`65d7323`的Python 3.12/3.13 CI完整功能回归各5994项：5878通过、116平台/能力跳过，
零失败/错误；之后均在12个Archive的许可证门禁失败。macOS、容器与资料Job成功，
Windows组合步骤仍截断；既不将两个Python结果累加，也不将整体CI或后继编排标记通过。
资料静态检查397份、10065个链接零问题；安全扫描含实际Wheel，共3565个输入零命中，
生成规格、Ruff及原可读性策略保持通过。
