# ADR 0074：Skill快照与Hook Action安全边界

- 状态：已接受
- 日期：2026-09-09
- 决策范围：Harnessix Code 0.8.5

## 背景

Skill和Hook提高Coding Agent的专业化与自动化能力，也会引入提示注入、名称劫持、路径越界、宿主命令执行、Secret泄露和供应链替换风险。Harnessix已有统一Action Plane，扩展不应获得第二条执行通道。

## 决策

### Skill

1. Skill被定义为只读、不受信内容包，不是可执行插件。
2. 宿主显式绑定本地来源Root；目录生成不可变摘要快照，正文和资源按需加载且每次复核内容摘要。
3. 普通名称只在全局唯一时解析；同名必须使用限定名称，来源内重复名称全部失效。
4. Skill正文与资源只通过`source="skill"`的`ExtensionActionPort`进入模型，不从Context层直接获得文件系统能力。
5. 不执行Skill脚本，不采信正文或Frontmatter中的Tool、Hook、Shell、权限和Secret声明。

### Hook

1. Hook处理器只能绑定宿主预注册的`source="hook"`只读Trusted Action；不支持任意宿主Shell、HTTP、Prompt或进程内第三方回调。
2. 非Bundled定义必须有绑定完整定义摘要的Trust Grant。定义变化使授权失效。
3. Hook输入只携带身份和摘要；输出经过尺寸、Schema与Secret检查。
4. `before_action`只能收紧执行，采用Blocking和Fail Closed；其他生命周期事件采用Advisory和Record Only。
5. Hook运行计划、状态和哈希链事件持久化；超时取消底层Action，重启后Running收敛为Interrupted且不自动重放。

## 被拒绝方案

### 在宿主进程中动态导入插件

拒绝。导入即获得宿主进程权限，无法依赖应用层Permission阻止文件、环境、网络和Secret访问。

### 直接执行Shell Hook

拒绝。即使设置超时，Shell仍能在Tool/Sandbox之前产生副作用，形成不可审计的旁路。

### 同名Skill按来源优先级覆盖

拒绝。静默覆盖允许低信任来源劫持模型已经看到的名称，也使恢复时无法证明实际加载对象。

### Skill自动注册Frontmatter声明的Tool或Hook

拒绝。内容包不能自行扩大能力；Tool和Hook必须由宿主建立独立可信绑定。

## 结果

### 正向结果

- Skill渐进加载与Hook自动化继续复用统一Policy、Sandbox、审批和Action审计；
- 目录、定义和运行都具备不可变摘要，可解释实际加载和执行版本；
- 恶意内容包不能通过符号链接、同名覆盖、Shell Hook或权限声明直接越权。

### 代价与限制

- 0.8.5不兼容依赖宿主Shell Hook或进程内插件的生态扩展；此类能力必须迁移为受管MCP/Container Action；
- Skill远端安装、签名发布、自动更新和Marketplace不在本切片；
- Interrupted Hook不自动重放，调用方需要以新的Dispatch显式重新触发生命周期事件。

## 验证要求

- 同名、内容漂移、符号链接、硬链接、敏感文件、超限Frontmatter与资源读取攻击测试；
- Trust Grant变更失效、Hook绑定漂移、顺序、Matcher、Blocking/Advisory与重复Dispatch测试；
- Hook超时、取消、Action失败、审批阻断、Secret输出、存储损坏和进程中断恢复测试；
- macOS、Linux和Windows导入及确定性单元测试，Linux/Windows句柄链平台测试。
