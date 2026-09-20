#!/usr/bin/env python3
"""从已完成私有Suite发布低敏真实Provider证据。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from harnessix.evals.cli_config import read_private_eval_config
from harnessix.evals.provider_suite_contracts import CodingEvalProviderSuiteRunConfig
from harnessix.evals.provider_suite_evidence import publish_provider_suite_evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="发布真实Provider Suite低敏证据")
    parser.add_argument("--config", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    config = read_private_eval_config(
        arguments.config,
        CodingEvalProviderSuiteRunConfig,
        max_bytes=2 * 1024 * 1024,
    )
    manifest = publish_provider_suite_evidence(config, arguments.evidence_root)
    print(manifest.model_dump_json())


if __name__ == "__main__":
    main()
