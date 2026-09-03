import json
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.smoke.contracts import SmokeReport
from tests.smoke.helpers import CANARY


@pytest.mark.parametrize("provider", ["openai_chat", "anthropic"])
def test_real_cli_sigint_settles_turn_and_cleans_session(provider, tmp_path):
    result_path = tmp_path / "result.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.smoke.interrupt_worker",
            provider,
            str(result_path),
            str(tmp_path / "config.json"),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert process.returncode == 130, process.stderr
    assert not process.stderr and CANARY not in process.stdout
    report = SmokeReport.model_validate_json(process.stdout)
    assert report.reason == "cancelled" and report.attempts_started is None
    snapshot = json.loads(result_path.read_text())
    assert snapshot["status"] == "cancelled"
    assert not Path(snapshot["workspace"]).exists()
