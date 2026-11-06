"""Harbor SWE-bench任务固定：锁文件拦住任何多出或改动的文件，准备阶段只追加派生镜像名。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.harbor import swebench_tasks

ENVIRONMENT = {
    "spec_version": "harnessix.harbor-task-environment/v1",
    "pypi_mirror": "https://pypi.example/simple",
    "python_mirror": "https://python.example",
    "managed_python": "3.12",
    "uv_wheel": {"filename": "uv.whl", "sha256": "0" * 64},
}


def upstream(root: Path) -> Path:
    task = root / "django__django-1"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "environment/Dockerfile").write_text("# c\nFROM swebench/sweb.eval.x86_64.x:latest\n")
    (task / "tests/test.sh").write_text("echo test\n")
    (task / "task.toml").write_text("[verifier]\ntimeout_sec = 1\n\n[environment]\ncpus = 1\n")
    return root


def locked(tmp_path: Path) -> tuple[Path, Path]:
    source = upstream(tmp_path / "upstream")
    lock = tmp_path / "tasks.lock.json"
    swebench_tasks.lock(source, [{"name": "django__django-1", "git_url": "g", "path": "p"}], lock)
    return source, lock


@pytest.mark.parametrize("change", ["apple_double", "edited"])
def test_lock_rejects_extra_or_changed_upstream_files(tmp_path: Path, change: str) -> None:
    source, lock = locked(tmp_path)
    task = source / "django__django-1"
    if change == "apple_double":
        (task / "._task.toml").write_bytes(b"\x00\x05\x16\x07")
    else:
        (task / "tests/test.sh").write_text("echo changed\n")
    with pytest.raises(SystemExit, match="不一致"):
        swebench_tasks._verify(source, json.loads(lock.read_text())["tasks"][0])


def test_prepare_builds_derived_image_and_only_adds_docker_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lock = locked(tmp_path)
    environment = tmp_path / "environment.json"
    environment.write_text(json.dumps(ENVIRONMENT))
    built: list[tuple[list[str], str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        built.append((command, Path(command[-1], "Dockerfile").read_text()))

    monkeypatch.setattr(swebench_tasks, "_uv_binaries", lambda env, workdir: None)
    monkeypatch.setattr(swebench_tasks, "_image_id", lambda ref: "sha256:" + "1" * 64)
    monkeypatch.setattr(swebench_tasks.subprocess, "run", fake_run)
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    receipt = swebench_tasks.prepare(source, lock, environment, ca, tmp_path / "prepared")
    task = receipt["tasks"]["django__django-1"]
    assert task["parent_image"] == "swebench/sweb.eval.x86_64.x:latest"
    assert task["image"].startswith("harnessix-harbor/django__django-1:")
    dockerfile = built[0][1]
    assert dockerfile.startswith("FROM swebench/sweb.eval.x86_64.x:latest\n")
    assert "COPY uv uvx /root/.local/bin/" in dockerfile
    assert "UV_INDEX_URL=https://pypi.example/simple" in dockerfile
    assert "ENV REQUESTS_CA_BUNDLE=/etc/harbor-offline/ca-bundle.pem" in dockerfile
    assert "/etc/ssl/certs/ca-certificates.crt /etc/harbor-offline/raw-mirror-ca.pem" in dockerfile
    prepared = (tmp_path / "prepared/django__django-1/task.toml").read_text()
    assert prepared == (
        '[verifier]\ntimeout_sec = 1\n\n[environment]\ndocker_image = "'
        + task["image"]
        + '"\ncpus = 1\n'
    )
    assert (tmp_path / "prepared/django__django-1/tests/test.sh").read_text() == "echo test\n"
