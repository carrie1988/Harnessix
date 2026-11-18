"""任务环境系统信息：探测输出解析、运行时记录不影响试次、补读只补缺失的试次，并与evalkit导入约定一致。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from evalkit import harbor as evalkit_harbor

from integrations.harbor import environment_facts
from integrations.harbor.environment_facts import ENVIRONMENT_FILE, backfill, record_environment

facts = environment_facts.environment_facts

DEBIAN = "Debian GNU/Linux\n12\nDebian GNU/Linux 12 (bookworm)\nLinux\nx86_64\n"


def test_file_name_and_spec_match_the_evalkit_import() -> None:
    assert ENVIRONMENT_FILE == evalkit_harbor.ENVIRONMENT_FILE
    assert environment_facts.ENVIRONMENT_SPEC == evalkit_harbor.ENVIRONMENT_SPEC


def test_probe_output_is_parsed_from_its_last_five_lines() -> None:
    parsed = facts("login banner\n" + DEBIAN, "runtime")
    assert parsed is not None
    assert (parsed["os"], parsed["os_pretty"], parsed["arch"], parsed["source"]) == (
        "Debian GNU/Linux 12",
        "Debian GNU/Linux 12 (bookworm)",
        "x86_64",
        "runtime",
    )
    assert parsed["host_kernel"] and parsed["host_arch"] and parsed["host_os"]
    without_release = facts("\n\n\nLinux\naarch64\n", "task_image")
    assert without_release is not None and without_release["os"] == "Linux"
    assert facts("Debian\n12\n\nLinux\n\n", "runtime") is None
    assert facts("x86_64\n", "runtime") is None


class FakeEnvironment:
    def __init__(self, result: object) -> None:
        self.result = result
        self.commands: list[str] = []

    async def exec(self, command: str, timeout_sec: int | None = None) -> object:
        self.commands.append(command)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_runtime_record_is_written_once_and_failures_write_nothing(tmp_path: Path) -> None:
    ok = FakeEnvironment(SimpleNamespace(return_code=0, stdout=DEBIAN))
    asyncio.run(record_environment(ok, tmp_path / "ok"))
    written = json.loads((tmp_path / "ok" / ENVIRONMENT_FILE).read_text(encoding="utf-8"))
    assert (written["os"], written["source"]) == ("Debian GNU/Linux 12", "runtime")
    assert ok.commands == [environment_facts.PROBE]
    for name, result in (
        ("failed", SimpleNamespace(return_code=1, stdout=DEBIAN)),
        ("raised", TimeoutError()),
        ("partial", SimpleNamespace(return_code=0, stdout="x86_64\n")),
    ):
        asyncio.run(record_environment(FakeEnvironment(result), tmp_path / name))
        assert not (tmp_path / name / ENVIRONMENT_FILE).exists()


def _trial(run: Path, arm: str, name: str, task: Path | None) -> Path:
    trial = run / "jobs" / arm / name
    trial.mkdir(parents=True)
    path = str(task) if task is not None else str(run / "no-such-task")
    (trial / "result.json").write_text(json.dumps({"config": {"task": {"path": path}}}))
    return trial


def test_backfill_only_fills_missing_records_and_probes_each_image_once(tmp_path: Path) -> None:
    task = tmp_path / "tasks" / "tinydb"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[environment]\ndocker_image = "nl2repo-tinydb:1"\n')
    broken = tmp_path / "tasks" / "broken"
    broken.mkdir()
    (broken / "task.toml").write_text('[environment]\ndocker_image = "broken:1"\n')
    run = tmp_path / "run"
    recorded = _trial(run, "harnessix", "tinydb__a", task)
    (recorded / "agent").mkdir()
    (recorded / "agent" / ENVIRONMENT_FILE).write_text('{"source": "runtime"}')
    _trial(run, "harnessix", "tinydb__b", task)
    _trial(run, "R", "tinydb__c", task)
    _trial(run, "R", "broken__d", broken)
    _trial(run, "R", "gone__e", None)
    (run / "jobs" / "R" / "running__f").mkdir()
    probed: list[str] = []

    def probe(image: str) -> str | None:
        probed.append(image)
        return DEBIAN if image == "nl2repo-tinydb:1" else None

    assert backfill(run, probe) == {"recorded": 1, "backfilled": 2, "unknown": 2}
    assert sorted(probed) == ["broken:1", "nl2repo-tinydb:1"]
    assert json.loads((recorded / "agent" / ENVIRONMENT_FILE).read_text()) == {"source": "runtime"}
    filled = json.loads((run / "jobs" / "R" / "tinydb__c" / "agent" / ENVIRONMENT_FILE).read_text())
    assert (filled["source"], filled["os"]) == ("task_image", "Debian GNU/Linux 12")
    assert not (run / "jobs" / "R" / "broken__d" / "agent").exists()
