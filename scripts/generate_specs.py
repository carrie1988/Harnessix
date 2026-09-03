from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from harnessix.agent.models import AgentEvent, Thread
from harnessix.api import create_app
from harnessix.domain.models import ActionRequest
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ProviderEvent
from harnessix.models.costs import CostReport
from harnessix.models.pricing import PriceSnapshot
from harnessix.smoke.contracts import SmokeConfig, SmokeReport


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    output = Path("spec")
    output.mkdir(exist_ok=True)
    write_json(output / "action-contract-v1.schema.json", ActionRequest.model_json_schema())
    write_json(output / "openapi.json", create_app().openapi())
    write_json(output / "agent-event-v5.schema.json", AgentEvent.model_json_schema())
    write_json(output / "agent-thread-v5.schema.json", Thread.model_json_schema())
    write_json(output / "provider-event-v3.schema.json", TypeAdapter(ProviderEvent).json_schema())
    write_json(output / "openai-chat-config-v1.schema.json", OpenAIChatConfig.model_json_schema())
    write_json(output / "anthropic-config-v1.schema.json", AnthropicConfig.model_json_schema())
    write_json(output / "price-snapshot-v1.schema.json", PriceSnapshot.model_json_schema())
    write_json(output / "cost-report-v1.schema.json", CostReport.model_json_schema())
    write_json(output / "model-smoke-config-v1.schema.json", SmokeConfig.model_json_schema())
    write_json(output / "model-smoke-report-v1.schema.json", SmokeReport.model_json_schema())
    print("已更新 Action、Agent、Provider、价格/成本、Smoke 和 OpenAPI Schema")


if __name__ == "__main__":
    main()
