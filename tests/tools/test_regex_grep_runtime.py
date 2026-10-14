"""major 4直接Runtime入口、旧descriptor字节摘要及Artifact归属。"""

import hashlib
import json
from uuid import UUID, uuid4

import pytest
import re2

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools import grep_matcher, search
from harnessix.tools.read_catalog import _build_definitions, _read_bindings
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import GrepSnapshotMatch, GrepSnapshotOutput
from harnessix.tools.workspace import digest
from tests.agent.helpers import answer
from tests.artifacts.helpers import results, step
from tests.tools.test_files import call, execute


def definitions(**flags):
    return _build_definitions(
        scope="a" * 64,
        implementation="posix-openat-nofollow/v1",
        max_concurrent_reads=4,
        require_approval=False,
        artifacts=None,
        git_runtime=None,
        read_bindings=_read_bindings(**flags),
    )


def test_legacy_and_snapshot_descriptors_are_byte_identical_to_prechange_sha():
    expected = {
        "list_files": "5cd1d18262754dd6bd27459d0ccad43a59125e43dc78ecaba934202e851e0e00",
        "read_file": "3b2692d74f2f4754a86605412656678906867cdf6e4f168dbad3b7d6b591e7ad",
        "glob": "f7b1f577311cb06997a1ef208d6490f199401263df3014d711a4f6e7e4a78622",
        "grep": "5bfdbee1cc598bc23846fe6f3e11ad012b2eafad603d989b47e7394839556fd7",
    }
    for name, definition in definitions().items():
        assert digest(definition.model_dump(mode="json")) == expected[name]
    assert hashlib.sha256(definitions()["grep"].model_dump_json().encode()).hexdigest() == (
        "5af624c512e44f02f79a16585f5144841cf501b8688cb26dd1350b4146f55acf"
    )
    snapshots = definitions(enable_grep_snapshots=True)
    expected["grep"] = "9ebc7ceebc2c758c1d022edfdaa66ff1c0d252c2b7cbd3078f3f529d1b6dd1fe"
    for name, definition in snapshots.items():
        assert digest(definition.model_dump(mode="json")) == expected[name]
    assert hashlib.sha256(snapshots["grep"].model_dump_json().encode()).hexdigest() == (
        "b76523c3e9fa84c22bece50bf33cae7cca68a034842c93c0630debef2498b159"
    )


def test_major4_only_replaces_grep_and_fingerprints_engine_semantics(monkeypatch):
    baseline = definitions()
    candidate = definitions(enable_regex_search=True)
    assert candidate["grep"].version.startswith("4.")
    assert all(candidate[name] == baseline[name] for name in baseline if name != "grep")
    schema = candidate["grep"].input_schema["properties"]
    assert schema["match_mode"]["default"] == "regex"
    assert schema["match_mode"]["enum"] == ["regex", "literal"]
    assert schema["context_lines"]["default"] == 2 and schema["max_results"]["default"] == 10
    contract = search.execution_contract(enable_regex_search=True)["regex"]
    assert contract["engine"] == "google-re2" and contract["version"] == "1.1.20251105"
    assert contract["engine_cache_max_mem"] == 128 * 1024 * 1024
    with monkeypatch.context() as changed:
        changed.setattr(grep_matcher, "RE2_MAX_MEM", 2 * 1024 * 1024)
        assert definitions(enable_regex_search=True)["grep"].version != candidate["grep"].version
        assert definitions() == baseline
    with monkeypatch.context() as changed:
        changed.setattr(grep_matcher, "RE2_VERSION", "future-version")
        assert definitions(enable_regex_search=True)["grep"].version != candidate["grep"].version
        assert definitions() == baseline


@pytest.mark.parametrize("invalid", [None, 1, "true", []])
def test_invalid_regex_flag_fails_before_workspace_open(tmp_path, invalid):
    with pytest.raises(KernelError) as raised:
        CodingToolRuntime(tmp_path / "missing", enable_regex_search=invalid)
    assert raised.value.code == "tool_regex_policy_invalid"


def test_two_search_flags_are_rejected_before_workspace_open(tmp_path):
    with pytest.raises(KernelError) as raised:
        CodingToolRuntime(
            tmp_path / "missing", enable_grep_snapshots=True, enable_regex_search=True
        )
    assert raised.value.code == "tool_search_policy_conflict"


async def test_direct_runtime_default_regex_explicit_literal_and_legacy_contract_rejection(
    tmp_path,
):
    body = b"class SyntheticNode(Base):\r\nclass OtherNode (Base):\r\na+b\r\naaab\r\n"
    (tmp_path / "source.py").write_bytes(body)
    async with CodingToolRuntime(tmp_path) as legacy:
        legacy_call = call(legacy, "grep", query="a+b")
        old = await legacy.execute(legacy_call, CancelToken())
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        with pytest.raises(KernelError) as raised:
            await tools.execute(legacy_call, CancelToken())
        assert raised.value.code == "tool_contract_changed"
        result = await execute(tools, "grep", query=r"^class\s+\w+\s*\(")
        literal = await execute(tools, "grep", query="a+b", match_mode="literal")
        regex = await execute(tools, "grep", query="a+b")
    output = GrepSnapshotOutput.model_validate_json(json.dumps(result.output))
    assert [hit.line for hit in output.matches] == [1, 2]
    assert all(hit.content_sha256 == hashlib.sha256(body).hexdigest() for hit in output.matches)
    assert all(hit.file_bytes == len(body) for hit in output.matches)
    assert [hit["line"] for hit in literal.output["matches"]] == [3]
    assert [hit["line"] for hit in regex.output["matches"]] == [4]
    assert old.output["matches"][0]["line"] == 3
    assert "content_sha256" not in old.output["matches"][0]


@pytest.mark.parametrize("query", ["(PRIVATE_MARKER", "(?=PRIVATE_MARKER)", r"(PRIVATE_MARKER)\1"])
async def test_invalid_regex_is_safe_correctable_runtime_result(tmp_path, query, capfd):
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        result = await execute(tools, "grep", query=query)
    assert result.outcome == "failed" and result.error.code == "tool_regex_invalid"
    assert "match_mode=literal" in result.error.message
    assert "PRIVATE_MARKER" not in result.model_dump_json()
    assert capfd.readouterr().err == ""


async def test_installed_version_drift_after_registration_is_a_safe_failure(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_bytes(b"needle\n")
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        monkeypatch.setattr(grep_matcher.metadata, "version", lambda _: "9.9.9")
        result = await execute(tools, "grep", query="PRIVATE_MARKER")
        literal = await execute(tools, "grep", query="needle", match_mode="literal")
    assert result.error.code == "tool_regex_unavailable"
    assert "PRIVATE_MARKER" not in result.model_dump_json()
    assert literal.outcome == "succeeded"


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "PRIVATE_MARKER" * 25},
        {"query": "PRIVATE_MARKER\n"},
        {"query": "needle", "match_mode": "PRIVATE_MARKER"},
        {"query": "needle", "context_lines": 4},
    ],
)
async def test_invalid_input_does_not_read_or_echo_values(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(search, "grep", lambda *a, **kw: pytest.fail("参数非法不得读取"))
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        result = await execute(tools, "grep", **arguments)
    assert result.error.code == "tool_invalid_arguments"
    assert "PRIVATE_MARKER" not in result.model_dump_json()


async def test_regex_mode_keeps_denied_symlink_and_hardlink_authority(tmp_path):
    (tmp_path / "secret.py").write_bytes(b"needle\n")
    (tmp_path / "visible.py").write_bytes(b"needle\n")
    (tmp_path / "linked.py").symlink_to("secret.py")
    (tmp_path / "hard.py").hardlink_to(tmp_path / "secret.py")
    async with CodingToolRuntime(
        tmp_path, denied_paths=("secret.py",), enable_regex_search=True
    ) as tools:
        result = await execute(tools, "grep", query="n(eed|xxx)le")
        denied = await execute(tools, "grep", query="needle", path="secret.py")
    assert [hit["path"] for hit in result.output["matches"]] == ["visible.py"]
    assert denied.error.code == "tool_path_denied"


async def test_legacy_runtime_remains_usable_without_re2(tmp_path, monkeypatch):
    monkeypatch.setattr(re2, "compile", lambda *a, **kw: pytest.fail("旧合同不依赖RE2"))
    (tmp_path / "source.py").write_bytes(b"a+b\n")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="a+b")
    assert result.outcome == "succeeded"


async def test_regex_artifact_preview_full_sha_and_owner_bounds(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    body = ("class SyntheticNode (Base): 中文\r\n" * 40).encode()
    (root / "source.py").write_bytes(body)
    store = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(store)
    async with CodingToolRuntime(root, artifacts=artifacts, enable_regex_search=True) as tools:
        provider = ScriptedProvider([step(query=r"^class\s+\w+\s*\("), answer()])
        async with AgentRuntime(
            store, provider, scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "合成源码搜索", request_id="regex")
        scope = tools.workspace_scope
    output = results(turn)[0].output
    preview = GrepSnapshotOutput.model_validate_json(json.dumps(output["preview"]))
    assert preview.truncated and len(preview.matches) == 10
    ref = output["artifact"]
    assert ref["complete"] and ref["records"] == 40
    page = await artifacts.read(thread.thread_id, scope, UUID(ref["artifact_id"]))
    pages = [page.text]
    while page.next_offset is not None:
        page = await artifacts.read(
            thread.thread_id, scope, UUID(ref["artifact_id"]), offset=page.next_offset
        )
        pages.append(page.text)
    full_text = "".join(pages)
    assert hashlib.sha256(full_text.encode()).hexdigest() == ref["sha256"]
    hits = [GrepSnapshotMatch.model_validate_json(line) for line in full_text.splitlines()]
    assert [hit.line for hit in hits] == list(range(1, 41))
    assert all(hit.content_sha256 == hashlib.sha256(body).hexdigest() for hit in hits)
    assert all(hit.file_bytes == len(body) for hit in hits)
    with pytest.raises(KernelError):
        await artifacts.read(uuid4(), scope, UUID(ref["artifact_id"]))
    with pytest.raises(KernelError):
        await artifacts.read(thread.thread_id, "f" * 64, UUID(ref["artifact_id"]))
