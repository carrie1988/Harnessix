---
doc_type: validation-evidence
status: current
version: 1
code_revision: 6daff318718c05f79752a5e6a873fe3cc9b80fb4
owners: [core]
modules: [delivery, workspace, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_material_directory_access.py
  - tests/product_config/test_git_material_native.py
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_snapshot_lifecycle.py
  - tests/product_config/test_git_material_owner_exit.py
  - tests/product_config/test_git_material_cas_integration.py
  - tests/delivery/test_git_tree_projection.py
  - tests/delivery/test_git_object_references.py
supersedes: []
---

# Windows材料目录访问与Git差分夹具整改验证包

## 1. 结论与范围

本包对应[总体与详细设计](../../changes/m09-r4-windows-material-directory-access.md)。
Windows私有终点开句柄按用途请求READ_CONTROL，缓存旧守卫持续持有，原ACL/身份/共享校验保持。
Git投影原两种格式真实差分复用已有隔离夹具，不把研究版本当成运行前置。
[Facts](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)和
[Manifest](manifest.json)分别绑定候选、实际结果、独立审查及公开资产。
本地固定候选验证已完成，新Windows原生结果未取得；不关闭R1/R4或商用发布。

## 2. 原失败与整改依据

[固定CI 36812510928](https://github.com/carrie1988/Harnessix/actions/runs/36812510928)六任务已终结：
文档/Container成功，三个macOS/Python矩阵各两项Git版本断言失败，Windows材料组25失败并达到原五分钟期限。
原Windows前两组通过，不能据此称全Job通过。原日志摘要保留，历史失败不改写。

目录链修复前六项测试3失败、1通过、2原生跳过；关联六文件后修98通过、4原生跳过。
Git投影/引用两文件430通过，实际运行Git2.53.0；不是实际2.55.0验收。
这些是冻结前局部结果，后续候选结果单独记录；不同集合和平台成绩不相加。
[官方GetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo)
证明READ_CONTROL前置，不证明原25项全部由此缺陷导致。

## 3. 不变合同与验证边界

原ACL、真实数据读取、share-read、Root/Owner、MAC/PID/EOF、8MiB对象、32MiB镜像和UNKNOWN不重放保持。
原CI全部选择器与3/5分钟期限保持，仅加入六项新目录访问回归。
原SHA1/SHA256完整blob/tree根与新增tree正文差分继续执行，命令失败/缺失能力仍失败；没有转skip。
没有新依赖、迁移、公共API、模型/凭据/费用账本变更或Docker操作。

## 4. 独立评审及图示

两文件独立静态审查未发现P0/P1/P2，前后完整字节一致；审查者函数测试及原生API调用均0。
四图与设计正文同源，实际渲染后按源码核对。首轮字体造成标签尾部裁切，调整换行和字体后重新检查；
句柄关闭回调位于成功CreateFileW之后、身份及ACL检查之前，失败仍由原作用域结算。
图示只证明源码对应与可读性，不代替Windows业务验收。

## 5. 尚未关闭的发布范围

新Windows材料链验收、完整Git产品Commit/Checkpoint及Backup v2、R3完整20 Trial、
Windows11消费者和独立Beta继续开放；本包不是商用GO。

## 6. 最终固定候选与实际发行包验证

完整代码输入1245件，目录SHA256为`dfe8e822cc981b971395ec0ad6baaca6d8f8822c1e658a65c3552001dddcf605`。
候选由基线6daff31加精确staged patch生成独立Git快照；未发布的诊断carrier完全排除，
不读取或复制用户未跟踪文件。输入在源码测试及单次发行包构建后保持一致。

| 范围 | 通过 | 跳过 | 失败/错误 |
| --- | ---: | ---: | ---: |
| 固定候选完整Delivery及六文件材料链关联 | 1065 | 27 | 0 |
| 固定候选完整治理 | 302 | 0 | 0 |
| 同一Wheel源码外Python3.12 | 528 | 4 | 0 |
| 同一Wheel源码外Python3.13 | 528 | 4 | 0 |

集合有重叠，不相加为独立场景或真实编码成绩。Format/Ruff、424模块Mypy、
原可读性、Schema及Task Pack检查通过；444文档、10897链接、937个Mermaid块零问题。
Secret六规则自检与仓库/实际Wheel扫描分别通过，3962输入完整覆盖、零命中。
最终公开包修改后文档/扫描另行复验，结果以Verification记录为准。

唯一Wheel SHA256：`1d2dd1e6d06ec7df7128f29d7a024714b5ca457a504c60cd6af6e82d45329e50`。
470 ZIP成员全RECORD验证，465包成员/424 Python模块与候选源码一致；两个源码外环境
全部包字节前后相同，各实际导入187模块，没有源码回退。安装依赖取自当前uv.lock，
带hash导出并强制hash安装；实际全部分发版本均符合锁文件。没有测试完整可选功能矩阵。

旧环境列表与当前锁文件anyio版本不同，在安装前被拒绝；初次离线安装因缺缓存失败，
保留两份原日志，仅从PyPI补足当前锁定且hash校验的依赖。没有改锁文件、重建Wheel或重写旧证据。
源码回归环境的实际分发版本单独记录，不把它冒称为这两个锁定安装环境。

差分夹具追加独立静态审查保持全部原核心AST断言，未发现P0/P1/P2；审查者函数测试0。
新原生CI、完整Git产品交付及R3真实质量仍须独立验收。
