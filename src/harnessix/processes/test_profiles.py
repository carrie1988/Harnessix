"""宿主预注册测试配置；模型只能选择名称，不能提交命令或argv。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.domain.errors import ToolNotFoundError
from harnessix.domain.models import (
    ActionSnapshot,
    EffectClass,
    Principal,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.processes.action_executor import ProcessActionExecutor
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.contracts import ProcessRequest, ProcessResult
from harnessix.processes.test_contracts import (
    RUN_TESTS_POLICY,
    RunTestsInput,
    TestProfile,
    TestProfiles,
)
from harnessix.runtime import ActionService
from harnessix.tools.workspace import digest


class RunTestsAgentBridge(ProcessAgentBridge):
    """把公开run_tests调用解析为唯一host.process Action。"""

    def __init__(
        self,
        service: ActionService,
        principal: Principal,
        workspace: Path,
        profiles: tuple[TestProfile, ...],
    ) -> None:
        try:
            checked = TestProfiles(profiles=tuple(sorted(profiles, key=lambda item: item.name)))
            root = workspace.resolve(strict=True)
        except (OSError, ValidationError, ValueError, TypeError):
            raise KernelError("test_profiles_invalid", "测试profile或工作区绑定无效") from None
        try:
            backend = service.registry.get("host.process")
        except ToolNotFoundError:
            raise KernelError("process_tool_not_found", "Action Plane缺少host.process") from None
        executor = backend.executor
        if not isinstance(executor, ProcessActionExecutor) or executor.workspace_root != root:
            raise KernelError("test_workspace_mismatch", "测试profile与进程Action工作区不一致")
        if any(
            profile.program not in executor.program_names
            or profile.timeout_seconds > executor.max_timeout_seconds
            for profile in checked.profiles
        ):
            raise KernelError("test_profiles_invalid", "测试profile超出宿主程序或时限绑定")
        self._profiles = checked
        self._workspace_root = root
        schema = RunTestsInput.model_json_schema()
        contract = digest(
            {
                "policy": RUN_TESTS_POLICY,
                "backend": backend.descriptor().model_dump(mode="json"),
                "workspace": str(root),
                "profiles": checked.model_dump(mode="json"),
                "input": schema,
            }
        )
        description = "运行宿主预注册测试profile；模型不能提交命令或参数。可选：" + "；".join(
            f"{profile.name}（{profile.description}）" for profile in checked.profiles
        )
        definition = ToolDescriptor(
            name="run_tests",
            version=f"{RUN_TESTS_POLICY}.{contract}",
            description=description,
            input_schema=schema,
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            requires_idempotency=True,
            requires_approval=True,
            supports_reconciliation=False,
        )
        super().__init__(
            service,
            principal,
            _definition=definition,
            _resolve=self._resolve_test_request,
        )

    def _resolve_test_request(
        self, call: ToolCallContent, scope: ToolExecutionScope
    ) -> ProcessRequest:
        try:
            matches = Path(scope.workspace).resolve(strict=True) == self._workspace_root
        except (OSError, ValueError):
            matches = False
        if not matches:
            raise KernelError("tool_workspace_mismatch", "测试调用与绑定工作区不一致")
        try:
            arguments = RunTestsInput.model_validate_json(
                json.dumps(call.arguments, ensure_ascii=False, allow_nan=False)
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("tool_invalid_arguments", "测试参数不符合严格契约") from None
        profile = self._profiles.get(arguments.profile)
        if profile is None:
            raise KernelError("test_profile_not_found", "测试profile未注册")
        return ProcessRequest(
            program=profile.program,
            arguments=profile.arguments,
            timeout_seconds=profile.timeout_seconds,
        )

    def result_output(
        self,
        call: ToolCallContent,
        snapshot: ActionSnapshot,
        process: ProcessResult | None,
    ) -> Any:
        output = super().result_output(call, snapshot, process)
        if process is None or not isinstance(output, dict):
            return output
        arguments = RunTestsInput.model_validate(call.arguments)
        return {
            "profile": arguments.profile,
            "passed": process.stop_reason == "exited" and process.returncode == 0,
            **output,
        }
