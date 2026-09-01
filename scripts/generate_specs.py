from __future__ import annotations

import json
from pathlib import Path

from harnessix.api import create_app
from harnessix.domain.models import ActionRequest


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    output = Path("spec")
    output.mkdir(exist_ok=True)
    write_json(output / "action-contract-v1.schema.json", ActionRequest.model_json_schema())
    write_json(output / "openapi.json", create_app().openapi())
    print("已更新 spec/action-contract-v1.schema.json 和 spec/openapi.json")


if __name__ == "__main__":
    main()
