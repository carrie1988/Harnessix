"""Langfuse v4公开实验API的实际字段形状、分页、异步可见性和误核验防护。"""

from __future__ import annotations

import copy
import json
from uuid import uuid4

import httpx
import pytest

from harnessix import cli
from harnessix.agent.errors import KernelError
from harnessix.evals.langfuse_projection import build_langfuse_suite_export
from harnessix.evals.langfuse_readback import verify_langfuse_export
from tests.evals.test_langfuse_export import suite_report


def remote_view(export):
    raw = json.loads(export.model_dump_json())
    spans = raw["resource_spans"][0]["scopeSpans"][0]["spans"]
    items = []
    for span in spans:
        attrs = {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}
        metadata = {
            key.removeprefix("langfuse.observation.metadata."): value
            for key, value in attrs.items()
            if key.startswith("langfuse.observation.metadata.")
        }
        scores = [
            {
                **score,
                "source": "API",
                "subject": {
                    "kind": "observation",
                    "id": score["observationId"],
                    "traceId": score["traceId"],
                },
            }
            for score in raw["scores"]
            if score["observationId"] == span["spanId"]
        ]
        items.append(
            {
                "id": span["spanId"],
                "traceId": span["traceId"],
                "experimentItemId": attrs["langfuse.experiment.item.id"],
                "experimentDatasetId": attrs["langfuse.experiment.dataset.id"],
                "input": attrs["langfuse.observation.input"],
                "output": attrs["langfuse.observation.output"],
                "metadata": metadata,
                "scores": scores,
            }
        )
    experiment = {
        "id": attrs["langfuse.experiment.id"],
        "name": attrs["langfuse.experiment.name"],
        "datasetId": attrs["langfuse.experiment.dataset.id"],
        "itemCount": len(items),
    }
    return experiment, items


def verify(export, handler, timeout=1):
    return verify_langfuse_export(
        export,
        base_url="http://127.0.0.1:3300/prefix",
        public_key="public",
        secret_key="private",
        timeout_seconds=timeout,
        transport=httpx.MockTransport(handler),
    )


def test_v4_metadata_strings_empty_cursor_page_and_complete_score_verification() -> None:
    export = build_langfuse_suite_export(suite_report())
    experiment, items = remote_view(export)
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["authorization"].startswith("Basic ")
        assert request.url.params["fromStartTime"]
        if request.url.path.endswith("/experiments"):
            return httpx.Response(200, json={"data": [experiment], "meta": {}})
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"data": [], "meta": {"cursor": "page-two"}})
        return httpx.Response(200, json={"data": items, "meta": {}})

    result = verify(export, handler)
    assert result.source_report_sha256 == export.source_report_sha256
    assert result.verified_trials == export.summary.scheduled_trials
    assert result.verified_scores == len(export.scores)
    assert len(paths) == 3
    assert all(path.startswith("/prefix/api/public/") for path in paths)


@pytest.mark.parametrize("flag", [True, False])
def test_v4_boolean_metadata_requires_exact_canonical_representation(tmp_path, flag: bool) -> None:
    from harnessix.evals.harbor_import import safe_import_harbor_job
    from harnessix.evals.langfuse_harbor_projection import build_langfuse_harbor_export
    from tests.evals.test_harbor_import import change_result, job_source

    directory = job_source(tmp_path / "job", ("pass",))
    change_result(
        directory,
        lambda data: data["agent_result"]["metadata"]["harnessix"].update(
            protocol_evidence_complete=flag
        ),
    )
    export = build_langfuse_harbor_export(safe_import_harbor_job(directory))
    experiment, items = remote_view(export)
    items[0]["metadata"]["protocol_evidence_complete"] = json.dumps(flag)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [experiment] if request.url.path.endswith("/experiments") else items,
                "meta": {},
            },
        )

    assert verify(export, handler).verified_trials == 1
    items[0]["metadata"]["protocol_evidence_complete"] = int(flag)
    with pytest.raises(KernelError) as error:
        verify(export, handler)
    assert error.value.code == "langfuse_readback_mismatch"


@pytest.mark.parametrize(
    "mode",
    [
        "duplicate_item",
        "trace",
        "dataset",
        "currency",
        "integer",
        "score_value",
        "score_bool",
        "score_time",
        "score_subject",
        "duplicate_score",
        "extra_score",
    ],
)
def test_readback_does_not_accept_wrong_or_duplicate_evidence(mode: str) -> None:
    export = build_langfuse_suite_export(suite_report())
    experiment, items = remote_view(export)
    item = items[0]
    if mode == "duplicate_item":
        items.append(copy.deepcopy(item))
    elif mode == "trace":
        item["traceId"] = "a" * 32
    elif mode == "dataset":
        item["experimentDatasetId"] = "different-dataset"
    elif mode == "currency":
        item["metadata"]["known_cost_currency"] = "CNY"
    elif mode == "integer":
        item["metadata"]["input_tokens"] = "0.0"
    elif mode == "score_value":
        item["scores"][0]["value"] = 999
    elif mode == "score_bool":
        item["scores"][0]["value"] = True
    elif mode == "score_time":
        item["scores"][0]["timestamp"] = "2020-01-01T00:00:00Z"
    elif mode == "score_subject":
        item["scores"][0]["subject"]["id"] = "other-observation"
    elif mode == "duplicate_score":
        item["scores"].append(copy.deepcopy(item["scores"][0]))
    else:
        extra = copy.deepcopy(item["scores"][0])
        extra["id"] = str(uuid4())
        item["scores"].append(extra)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [experiment] if request.url.path.endswith("/experiments") else items,
                "meta": {},
            },
        )

    with pytest.raises(KernelError) as error:
        verify(export, handler)
    assert error.value.code == "langfuse_readback_mismatch"


def test_pending_data_times_out_without_post_or_agent_reexecution() -> None:
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"data": [], "meta": {}})

    with pytest.raises(KernelError) as error:
        verify(build_langfuse_suite_export(suite_report()), handler, timeout=0.01)
    assert error.value.code == "langfuse_readback_timeout"
    assert calls and set(calls) == {"GET"}


def test_readback_failure_does_not_expose_response_body() -> None:
    with pytest.raises(KernelError) as error:
        verify(
            build_langfuse_suite_export(suite_report()),
            lambda _: httpx.Response(401, text="PRIVATE-READBACK-BODY"),
        )
    assert "PRIVATE-" not in str(error.value)
    assert error.value.code == "langfuse_readback_rejected"


def test_cli_readback_requires_explicit_network_before_reading_source_or_credentials(
    monkeypatch, capsys
) -> None:
    from harnessix.evals import export_cli

    original_get = export_cli.os.environ.get

    def guarded_get(key, default=None):
        if key.startswith("LANGFUSE_"):
            pytest.fail("credential read")
        return original_get(key, default)

    monkeypatch.setattr(export_cli.os.environ, "get", guarded_get)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "coding-eval-export",
                "--suite-report",
                "/missing/private.json",
                "--output",
                "/missing/export.json",
                "--verify",
            ]
        )
    assert json.loads(capsys.readouterr().out)["reason"] == "langfuse_readback_arguments_invalid"
