from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from harnessix.agent.models import AgentEvent, Thread
from harnessix.api import create_app
from harnessix.domain.models import ActionRequest
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ProviderEvent


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    output = Path("spec")
    output.mkdir(exist_ok=True)
    write_json(output / "action-contract-v1.schema.json", ActionRequest.model_json_schema())
    write_json(output / "openapi.json", create_app().openapi())
    write_json(output / "agent-event-v3.schema.json", AgentEvent.model_json_schema())
    write_json(output / "agent-thread-v3.schema.json", Thread.model_json_schema())
    write_json(output / "provider-event-v1.schema.json", TypeAdapter(ProviderEvent).json_schema())
    write_json(output / "openai-chat-config-v1.schema.json", OpenAIChatConfig.model_json_schema())
    print("已更新 Action、Agent、Provider Event/Config 和 OpenAPI Schema")


if __name__ == "__main__":
    main()
