---
doc_type: change-design
status: draft
version: 1
code_revision: ae560ad26899ae8ecb55bfc5c0ff2539727f5088
owners: [core]
modules: [product_config, session, secrets]
related_adrs:
  - docs/adr/0104-managed-session-key-and-default-root.md
related_tests:
  - tests/product_config/test_session_key.py
  - tests/product_config/test_session_key_dpapi.py
  - tests/product_config/test_session_key_windows.py
  - tests/product_config/test_managed_session_root.py
supersedes: []
---

# 0.9.4a 默认产品持久Session密钥总体与详细设计

## 1. 需求背景、设计目标与状态

真实SQLite同事务认证仍需要可信宿主提供稳定独立密钥。仅有库级Binding，不能关闭默认产品
原未知历史在当前Scope下被当作安全事实消费的缺口；重启生成临时Key会破坏合法恢复。
本设计在正式Product Server强制装配独立本机Key Backend，并在Provider构造和开放协议之前验证Session。

目标：Key/逻辑Store身份跨重启稳定；不使用模型凭据；新密钥先持久化再初始化认证库；
原Key缺失/失效或历史未证明时失败关闭；不删除、改写或补签原历史；取消后结算唯一IO任务。

| 部分 | 实现与验收状态 |
|---|---|
| POSIX私有FD、owner/mode/单硬链接与原子发布 | 已实现；实际本机文件、锁、进程退出和恢复测试 |
| macOS扩展ACL | 已实现；实际Native API与三个对象ACL篡改测试；不是只检查mode bits |
| Windows用户DPAPI、原生Owner/受保护DACL/句柄链 | 已实现并提供真实Windows测试入口；本机跳过，不声明Windows通过 |
| 默认Root强制Binding、先验原库再构造Provider | 已实现；实际配置/Scope/Store/Runtime和SDK测试 |
| Key备份导出/轮换/跨机器迁移、维护CLI装配 | 未完成 |
| Artifact正文/二进制持久来源证明与跨Epoch重开 | 未完成，原Epoch不放宽 |
| 全部Provider/Owner/SDK相关ID、0.9.4～0.9.6与发布 | 未完成；12件Archive来源权利继续阻断 |

## 2. 信任边界与非目标

攻击者拥有SQLite文件写权限但不能读取或替换独立Key时，不能伪造新原事实、投影或来源证明。
Key不进入Session DB、Product Config快照、Workspace、模型请求、Protocol、Artifact或诊断包。
POSIX采用规范私有本机文件，不声称Key正文加密；Windows采用当前用户DPAPI，不使用机器作用域。

不防同UID任意代码、拥有Key权限的插件、系统管理员、完整有效库回滚、整Thread及证明共同删除；
不认证Tenant/物理数据库身份，不提供Session正文加密、任意DLP或硬抢占OS文件系统调用。
独立生成不是Keyring不可导出保护；当前托管策略不得被描述为密钥备份、轮换或三平台正式安装完成。

## 3. 总体架构与系统上下文

```mermaid
flowchart TD
  Product[Product Server启动预检] --> Scope[冻结Provider公开保护Scope]
  Scope --> Load[受托唯一Key加载任务]
  Load --> OS{真实宿主平台}
  OS --> POSIX[私有FD与权限 macOS ACL]
  OS --> Windows[用户DPAPI 原生Owner DACL]
  POSIX --> Key[稳定Store ID Key ID 独立32字节Key]
  Windows --> Key
  Key --> Binding[托管SessionPublicationBinding]
  Binding --> Session[初始化并验证真实Session]
  Session --> Provider[构造托管Provider Bundle]
  Provider --> Runtime[Tools Action Agent Runtime]
  Runtime --> Protocol[激活配置 开放stdio]
  Fail[缺Key 未证明或失效] -.-> Deny[有限失败 不开放协议]
  Session -.-> Fail
  Load -.-> Fail
```

平台由实际`os.name`决定，不接受配置声明把Windows伪装成POSIX或失败后降级明文。
原`SQLiteSessionStore`和`SessionPublicationBinding`复用，不复制Store、增加HTTP/Worker或新模型凭据系统。
原配置审计可记录公开配置加载事实；拒绝历史发生在模型工厂和Protocol入口之前。

## 4. 私有目录、持久化与Key发布流程

```mermaid
flowchart TD
  Begin[验证Root与私有目录 原生对象身份] --> Lock[取得独立非阻塞初始化锁]
  Lock --> Exists{正式Key存在}
  Exists -->|存在| Verify[只读验证原对象和闭合Codec]
  Exists -->|不存在| DB{原DB WAL SHM是否存在}
  DB -->|存在| Refuse[拒绝生成替代身份 保留原库]
  DB -->|不存在| Pending{完整原候选是否存在}
  Pending -->|存在| Recover[验证并复用原候选 不重新随机生成]
  Pending -->|不存在| Create[独立RNG Store Key身份 编码 写候选并同步]
  Recover --> Publish[不可覆盖发布 同步平台支持的提交]
  Create --> Publish
  Publish --> Verify
  Verify --> Return[返回自有Key材料 释放文件和锁]
```

布局为产品私有状态中的`session-auth/key.v1`、`.lock`和短期`key.v1.pending`，均不在Workspace。
首次生成之前在同一初始化锁内检查` sessions.db / -wal / -shm `存在性；即使旧库看起来为空，
也不把缺Key解释为首次运行。旧版升级需要后续正式隔离/迁移流程或独立新状态，不能清空旧库。

### 4.1 POSIX

以Root目录FD打开私有子目录；`O_NOFOLLOW/O_CLOEXEC`、owner、目录700、文件600和单硬链接全部验证。
不自动chmod危险既有Key。Darwin另使用SDK核对的`acl_get_fd_np(ACL_TYPE_EXTENDED)`，
拒绝任何既有扩展ACL，不能把mode 600/700当作macOS ACL保障。
Linux权限模型下group/other为零，扩展访问ACL的有效mask受该模式约束；未声明所有网络文件系统支持。

候选`O_EXCL`创建、完整写入、文件fsync与目录fsync后，通过`link`不可覆盖发布。
若进程在正式名字建立但候选尚未移除时退出，两名字必须是同一安全inode且恰为双硬链接，
才允许在锁内结算候选；陌生硬链接、损坏Codec或不同对象不修复。原Key字节不重新生成。

### 4.2 Windows

复用`WindowsWorkspaceRoot`逐段句柄链，拒绝Reparse Point/Junction，并在加载期间固定私有路径。
新目录/文件显式建立当前Owner及仅当前用户/SYSTEM全权限的protected DACL；
既有对象用`GetSecurityInfo`、DACL control和实际ACE类型/flags/mask/SID核对，不使用chmod证明ACL。
文件必须单硬链接、不是目录或reparse；读取有8192字节上限，前后实际File Index/Revision一致。

DPAPI使用用户作用域与`CRYPTPROTECT_UI_FORBIDDEN`，不给Prompt、不设置LOCAL_MACHINE，
固定应用用途熵不是第二密钥。解密/权限失败不能改用明文、其他用户或另一Store Key。
候选由原生`WriteFile/FlushFileBuffers`保存，`MoveFileExW`不允许覆盖或跨卷复制。
不声称Windows目录fsync或断电证明；明确拒绝UNC状态，NTFS实际API验收另行记录。

## 5. Key数据结构与用途

| 编码 | 字段 | 长度与语义 |
|---|---|---|
| 内部payload | HXSK＋版本1、Store UUID、Key UUID、原Key | 5＋16＋16＋32＝69字节，闭合无尾随数据 |
| POSIX封套 | HXKP＋版本1、payload | 5＋69＝74字节，私有文件不是加密 |
| Windows封套 | HXKW＋版本1、Cipher长度LE32、用户DPAPI Cipher | 9＋Cipher，长度字段等于剩余字节；总上限8192 |
| OwnedSessionKey | store_id/key_id/key bytearray | 稳定逻辑身份；Key `repr=False`，close尽力清零自有副本 |
| SessionPublicationBinding | 原Key及Scope的自有副本 | 既有Header/Event/Projection用途域和MAC不变 |

UUID与Key由独立OS随机源生成，不用Provider API Key派生；不依赖Profile/凭据版本或目录路径。
Key不旋转于每次重启、凭据轮换或新Scope。Windows用户/机器迁移可能无法解密，不能宣称直接复制文件即可恢复。

## 6. 类与接口设计、源码职责

| 源码/接口 | 输入与输出 | 单一职责 |
|---|---|---|
| session_key_codec.py | 原69字节→OwnedSessionKey；生成新payload | 闭合二进制、独立身份、自有材料生命周期 |
| session_key_store.py::load_session_key | 私有Root/fault→OwnedSessionKey | 真实平台选择、统一有限失败，不降级 |
| session_key_posix.py::load_posix_key | Root及内部fault→原payload | FD锚定、权限/ACL、锁、不可覆盖提交/恢复 |
| session_key_dpapi.py::transform | 有界bytes/unprotect→bytes | 原生用户DPAPI ABI、结果释放和清零 |
| session_key_windows_security.py::PrivateKeySecurity | Native APIs/handle | 原Owner与protected DACL验证/新对象描述符 |
| session_key_windows_files.py::WindowsKeyFiles | 既有WindowsRoot/原路径 | 句柄与权限端口；有限IO拆为模块函数，不提高结构阈值 |
| session_key_windows.py::load_windows_key | 原Root/fault→payload | 原生文件/用户DPAPI组合、候选生命周期 |
| session_key.py::open_product_session_binding | 原Root/PublicationScope→托管Binding | 唯一线程任务、取消/超时结算和全部自有Key清理 |
| server.py::_serve_product_stdio | 已预检Startup | Key/Session先验真，然后Provider/Runtime/Protocol的生命周期 |

私有结构不是公共Secret Provider，现有配置Schema、Session Schema29、Event Codec、公共DTO和导出不变。
`fault`仅用于可信测试注入，不来自配置、Protocol或模型。既有平台安全路径端口被复用，不复制通用路径实现。

## 7. 产品启动、读取与失败时序

```mermaid
sequenceDiagram
  participant Host as Product启动
  participant Key as 唯一Key任务
  participant Store as 真实Session
  participant Bundle as Provider Bundle
  participant User as SDK或UI
  Host->>Key: 规范私有Root与冻结Scope
  Key-->>Host: 稳定独立原身份
  Host->>Store: Binding initialize
  alt 缺Key 未证明历史或错误身份
    Store--xHost: 有限失败
    Host->>Key: 清零自有副本
    Note over Bundle,User: 不构造Provider 不开放Protocol
  else 原Header与历史可消费
    Host->>Bundle: 构造并进入托管生命周期
    Host->>Store: Runtime恢复时认证原Snapshot和前缀
    Host->>User: 激活并开放stdio
    User->>Store: 查询或追加原认证事实
    Host->>Bundle: 关闭Provider资源
    Host->>Key: 清零Binding与材料
  end
```

Session初始化先验证Store头；Runtime启动恢复再认证全部Snapshot。损坏投影及普通SHA同时替换也会拒绝，
可能已经构造的Bundle仍完整关闭，模型请求为零。未知旧历史在更早阶段被拒绝，无Provider/Protocol消费。
原认证历史在新Scope下可私有重开，但当前材料与公开权限检查仍不可省略。

### 7.1 并发追加与认证读版本

产品启用认证后，审批等待中的SDK事件重放与Runtime追加可以同时发生。
原独立`events`入口没有显式读事务，多条SELECT可能分别取得旧Checkpoint和新Event/Seal，
使合法原历史在完整前缀检查中被误判为`publication_history_unproven`。
`get/list/recovery`已有读事务，`append/rebuild/Fork`已有写事务；不增加另一套锁或降级证明。

当前`SQLiteSessionStore.events`在任何认证读取前执行`BEGIN`，所有Checkpoint、Event与Seal
来自同一SQLite快照。读者可返回其固定旧版本，后续新读才观察完整新CAS，不自动重试或重签。
两个确定性测试在读者取得Checkpoint后，让另一真实连接先提交，再继续读原事件；
分别覆盖`after=0`和`after=1`，并检查新事实及Seal完整提交。旧实现两项均失败，修改后通过。
这不是消除篡改检查：Snapshot/Event/Seal损坏与缺失依然必须拒绝。

## 8. 取消、超时、进程退出与恢复语义

```mermaid
sequenceDiagram
  participant Parent as 调用方
  participant Owner as Key Owner
  participant Thread as 唯一受托线程
  Parent->>Owner: 打开Binding
  Owner->>Thread: shield唯一加载任务
  alt 父取消或5秒准入期限到达
    Parent--xOwner: CancelledError或Timeout
    Owner->>Thread: 等待同一任务结算 不重启不强杀
    Thread-->>Owner: 原结果或有限错误
    Owner->>Owner: 清零返回材料 不发布Binding
    Owner--xParent: 自然取消或publication_key_timeout
  else 正常结果
    Thread-->>Owner: 自有Key
    Owner-->>Parent: 托管Binding
    Parent->>Owner: 关闭或任意异常
    Owner->>Owner: 清零Binding两份与材料
  end
```

5秒是准入期限，不是硬抢占阻塞OS调用；清理可以延后完成，以避免遗留线程或丢弃返回Key。
父取消优先传播，不用重试掩盖未知线程。非阻塞锁占用是busy，不自动生成替代Key。
进程退出前候选与正式提交后Key分别有实际OS测试；不当作断电、Windows平台通过或Key迁移验收。

| 错误 | 语义/副作用 |
|---|---|
| publication_key_unavailable | 缺失、权限/ACL/格式/身份不可用；不覆盖Key、不补签旧库 |
| publication_key_busy | 独立初始化锁正在占用；无另一密钥、无Protocol开放 |
| publication_key_timeout | 准入超时并结算唯一任务；可能已安全持久Key但不发布Binding |
| publication_history_unproven | 原Store/历史缺证明或MAC不匹配；原事件/证明不改写 |
| 原Scope失效有限码 | 无Binding/新签发；已返回材料清零 |
| 父CancelledError | 自然取消，线程与文件资源结算，自有Key清零 |

## 9. 核心代码业务伪代码

```text
load_key:
  真实平台端口验证私有原对象；取得独立锁
  若正式Key存在：只读原封套、验证身份与闭合格式
  否则若任何原DB/WAL/SHM存在：拒绝，不生成、不删除
  否则复用完整原候选，或独立随机生成；不可覆盖发布
  返回自有材料；释放文件、目录、锁、ACL与Native缓冲

open_binding:
  唯一受托线程在shield下加载
  若父取消或准入超时：等待同一线程，清零其结果，传播有限语义
  正常以原Key/Store身份和Scope建立既有Binding
  任意退出finally清零Binding与材料

product_start:
  预检Workspace/状态/配置；冻结选定Provider材料
  打开稳定Binding；初始化并验证Session
  构造托管Provider；Tools/Action/Runtime恢复验真
  仅全部成功才激活配置并开放Protocol
```

## 10. 部署、升级、备份、回退与兼容

新安装需本机私有状态：macOS/Linux目录700、无Darwin扩展ACL；Windows当前用户NTFS及原生私有DACL。
状态与Workspace不重叠。Key丢失不能重新随机生成并追认旧库，应保留原数据、使用后续正式恢复流程。
存在旧DB但无Key会拒绝；现阶段不提供清空、补签或把旧文件复制进新认证库的自动升级捷径。

仅复制Session DB不等于可恢复；现有维护CLI尚未装配托管Key，不能用未绑定Store读取/修改认证库。
Key导出、保护恢复包、轮换、跨用户/机器迁移、卸载与诊断包敏感输入规则必须在后续切片完成。
回退到无保护旧程序不是安全回退；库级无认证模式只用于显式宿主/旧数据隔离，不是默认产品降级选项。

## 11. 可观测性、验证测试与剩余风险

公开观测只记录有限码、版本、计数、摘要、布尔不变事实和精确Revision，不保存原Key、原Prompt或供应商正文。
测试分开记录实际文件/ACL/锁/OS退出、真实Root/SDK、DPAPI ABI替身与Windows原生测试；替身不证明Windows。
真实模型请求0次。当前六类Windows测试必须在实际Windows运行；真实三平台安装、Beta与来源权利仍开放。

测试：原Key重开、错封套/版本/长度、缺Key/DB sidecar、原候选与正式发布退出、锁占用、硬链接/符号链接/ACL、
父取消、超时单任务回收、失效Scope、Root双重启新Turn、旧未证明历史拒绝、投影+SHA篡改及直接Service新材料拒绝。
候选发行物与完整回归在固定源码上单独冻结，不能从测试文件存在推导生产或三平台通过。

## 12. 源码阅读顺序

1. [Codec](../../src/harnessix/product_config/session_key_codec.py)与[平台Facade](../../src/harnessix/product_config/session_key_store.py)。
2. [POSIX](../../src/harnessix/product_config/session_key_posix.py)：目录FD、原文件、锁、候选与不可覆盖发布。
3. [DPAPI](../../src/harnessix/product_config/session_key_dpapi.py)、[ACL](../../src/harnessix/product_config/session_key_windows_security.py)、
   [原生文件](../../src/harnessix/product_config/session_key_windows_files.py)、[Windows组合](../../src/harnessix/product_config/session_key_windows.py)。
4. [Owner](../../src/harnessix/product_config/session_key.py)与[默认Root](../../src/harnessix/product_config/server.py)。
5. [既有SQLite认证设计](m09-4a-authenticated-sqlite-session.md)与[来源研究](../research/managed-session-key-and-root.md)。
