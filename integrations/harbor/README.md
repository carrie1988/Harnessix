# Harnessix Harbor适配器

Harbor 0.25.0的独立适配，通过公共SDK驱动产品Agent Protocol。
依赖不进入产品uv.lock；任务镜像必须预装Harnessix及原生配置。

完整操作、权限与未知费用范围见[评测接入说明](../../docs/operations/evaluation-integrations.md)。

验证命令：

```bash
uv run --frozen pytest tests/evals/test_langfuse_export.py tests/integrations/test_harbor_protocol.py
integrations/harbor/.venv/bin/python -m pytest tests/integrations/test_harbor_adapter.py
```

第二条命令需要在隔离环境中安装测试依赖；如需测试公共SDK，同时以editable或wheel方式安装本项目。
默认产品测试不要求安装Harbor，可跳过可选适配器测试。

任务工作区外保存配置、状态和输入。遇到审批或问题即停止并请求取消，禁止通过自动回答扩展权限。
重复试次必须使用新身份；旧试次需要原恢复流程，协议驱动不会以重新执行补齐结果。
Oracle/Nop夹具的1/0结果是环境验收控制，不是Harnessix编码成绩。
