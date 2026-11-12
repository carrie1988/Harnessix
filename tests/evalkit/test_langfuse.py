"""Langfuse投影、发送与读回：全部槽位、稳定身份、白名单字段、未知值保持未知、失败不重试。"""

from __future__ import annotations

import copy
import json
import math
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
import pytest
from evalkit import cli
from evalkit.contracts import (
    ArmSpec,
    Cost,
    DatasetItem,
    DatasetManifest,
    EvalRunReport,
    GraderSpec,
    RunPlan,
    Score,
    TrialRecord,
    Usage,
    summarize,
)
from evalkit.errors import EvalkitError
from evalkit.files import write_model
from evalkit.langfuse import LangfuseExport, build_export, read_back, send
from evalkit.steps import Step, StepTrace

from tests.evalkit.builders import CONFIG, PLAN_CREATED, task_digest

GRADER = GraderSpec(grader_id="suite", version="1", pass_score="task_passed")
METADATA = "langfuse.observation.metadata."


def report(*, provenance: str = "preregistered") -> EvalRunReport:
    plan = RunPlan(
        run_name="smoke",
        dataset=DatasetManifest(
            name="d",
            version="1",
            items=tuple(
                DatasetItem(item_id=f"case-{i}", item_digest=task_digest(i)) for i in (0, 1)
            ),
        ),
        arms=tuple(
            ArmSpec(
                arm_id=arm, system=arm, system_version="v1", config_digest=CONFIG, executor="import"
            )
            for arm in ("harnessix", "baseline")
        ),
        attempts_per_item=1,
        graders=(GRADER,),
        primary_grader="suite",
        provenance=provenance,
        created_at=PLAN_CREATED,
    )
    start = PLAN_CREATED + timedelta(minutes=5)
    trials = []
    for slot, (arm, item, attempt) in enumerate(plan.slots()):
        if slot == 3:
            trials.append(
                TrialRecord(
                    slot=slot,
                    arm_id=arm,
                    item_id=item,
                    attempt=attempt,
                    status="missing",
                    reason="missing_result",
                )
            )
            continue
        trials.append(
            TrialRecord(
                slot=slot,
                arm_id=arm,
                item_id=item,
                attempt=attempt,
                status="completed",
                reason="completed",
                scores=(Score(name="task_passed", value=slot == 0, grader_id="suite"),),
                usage=Usage(
                    completeness="complete", model_calls=3, input_tokens=100, output_tokens=7
                ),
                cost=Cost(completeness="complete", currency="CNY", amount="0.000123456789")
                if slot == 0
                else Cost(),
                started_at=start,
                completed_at=start + timedelta(seconds=90),
                system_refs={"turn_id": f"turn-{slot}"},
            )
        )
    return EvalRunReport(
        plan=plan,
        plan_digest=plan.digest,
        trials=tuple(trials),
        summary=summarize(plan, tuple(trials)),
        completed_at=start + timedelta(hours=1),
    )


def spans_of(export: LangfuseExport) -> list[dict[str, Any]]:
    raw = json.loads(export.model_dump_json())
    return raw["resource_spans"][0]["scopeSpans"][0]["spans"]


def attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


# ---------------------------------------------------------------- 投影


def test_projection_keeps_every_slot_pairs_arms_and_is_stable() -> None:
    export = build_export(report())
    assert export == build_export(report())
    spans = [attrs(span) for span in spans_of(export)]
    assert len(spans) == 4
    experiments = {s["langfuse.experiment.id"] for s in spans}
    assert len(experiments) == 2
    items = [s["langfuse.experiment.item.id"] for s in spans]
    assert items[0] == items[2] and items[1] == items[3] and items[0] != items[1]
    assert spans[3][METADATA + "status"] == "missing"
    assert spans[3][METADATA + "time_source"] == "report_window"
    outcomes = [s["value"] for s in export.scores if s["name"] == "outcome"]
    assert outcomes == ["passed", "failed", "failed", "invalid"]
    backfilled = attrs(spans_of(build_export(report(provenance="backfilled")))[0])
    assert backfilled["langfuse.experiment.name"].startswith("backfilled/smoke/harnessix/")
    assert backfilled[METADATA + "provenance"] == "backfilled"


def test_v2_plan_exports_grader_outcome_and_stability_outcome() -> None:
    """v2主指标只看判分：预算耗尽但判分通过的试次outcome为passed，normal_end_outcome为failed。"""
    source = report()
    plan = source.plan.model_copy(update={"spec_version": "evalkit.run-plan/v2"})
    first = source.trials[0].model_copy(update={"status": "system_error", "reason": "budget"})
    trials = (TrialRecord.model_validate(first.model_dump()), *source.trials[1:])
    v2 = EvalRunReport(
        plan=plan,
        plan_digest=plan.digest,
        trials=trials,
        summary=summarize(plan, trials),
        completed_at=source.completed_at,
    )
    export = build_export(v2)
    by_name: dict[str, list[Any]] = {}
    for score in export.scores:
        by_name.setdefault(score["name"], []).append(score["value"])
    assert by_name["outcome"] == ["passed", "failed", "failed", "invalid"]
    assert by_name["normal_end_outcome"] == ["failed", "failed", "failed", "invalid"]
    assert "normal_end_outcome" not in {s["name"] for s in build_export(source).scores}


def test_unknown_cost_stays_missing_and_currency_is_not_converted() -> None:
    spans = [attrs(span) for span in spans_of(build_export(report()))]
    assert spans[0][METADATA + "cost_currency"] == "CNY"
    assert spans[0][METADATA + "cost_amount"] == "0.000123456789"
    assert spans[1][METADATA + "cost_completeness"] == "unknown"
    assert METADATA + "cost_amount" not in spans[1]
    assert not any("usd" in key.lower() for span in spans for key in span)


def test_projection_uses_contract_fields_only() -> None:
    source = report()
    trial = source.trials[0].model_copy(update={"system_refs": {"turn_id": "PRIVATE-PROMPT"}})
    assert "PRIVATE-PROMPT" in json.dumps(
        attrs(
            spans_of(
                build_export(source.model_copy(update={"trials": (trial, *source.trials[1:])}))
            )[0]
        )
    )
    keys = {
        key.removeprefix(METADATA) for span in spans_of(build_export(source)) for key in attrs(span)
    }
    assert not {"input_text", "output_text", "prompt", "patch", "error"} & keys


def test_projection_rejects_tampered_reports() -> None:
    source = report()
    forged = source.model_copy(
        update={"summary": tuple(s.model_copy(update={"passed": 2}) for s in source.summary)}
    )
    with pytest.raises(ValueError):
        build_export(forged)


# ---------------------------------------------------------------- 发送


def accept_all(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/traces"):
        return httpx.Response(200, json={})
    body = json.loads(request.content)
    return httpx.Response(
        207,
        json={"successes": [{"id": e["id"], "status": 201} for e in body["batch"]], "errors": []},
    )


def test_transport_sends_otlp_then_timestamped_score_batches_with_stable_ids() -> None:
    export = build_export(report())
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        assert request.headers["authorization"].startswith("Basic ")
        assert request.headers["x-langfuse-ingestion-version"] == "4"
        return accept_all(request)

    for _ in range(2):
        send(
            export,
            base_url="http://localhost:3000/prefix",
            public_key="public",
            secret_key="private",
            transport=httpx.MockTransport(handler),
        )
    midpoint = len(requests) // 2
    assert requests[:midpoint] == requests[midpoint:]
    assert requests[0][0] == "/prefix/api/public/otel/v1/traces"
    events = [event for _, body in requests[1:midpoint] for event in body["batch"]]
    assert len(events) == len(export.scores)
    assert all("timestamp" not in event["body"] for event in events)


@pytest.mark.parametrize(
    "mode", ["redirect", "partial_trace", "partial_score", "timeout", "oversize"]
)
def test_transport_rejects_partial_acceptance_and_does_not_retry_or_leak(mode: str) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if mode == "timeout":
            raise httpx.ConnectTimeout("PRIVATE-SERVER-BODY", request=request)
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://elsewhere.invalid"})
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * 70_000)
        if mode == "partial_trace":
            return httpx.Response(200, json={"partialSuccess": {"rejectedSpans": "1"}})
        if len(calls) == 1:
            return httpx.Response(200, json={})
        return httpx.Response(207, json={"errors": ["PRIVATE-SERVER-BODY"], "successes": []})

    with pytest.raises(EvalkitError) as failure:
        send(
            build_export(report()),
            base_url="https://example.invalid",
            public_key="public",
            secret_key="private",
            transport=httpx.MockTransport(handler),
        )
    assert "PRIVATE-SERVER-BODY" not in str(failure.value)
    assert len(calls) == (2 if mode == "partial_score" else 1)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com?key=private",
        "https://example.com#fragment",
    ],
)
def test_transport_validates_endpoint_before_any_request(url: str) -> None:
    with pytest.raises(EvalkitError) as error:
        send(
            build_export(report()),
            base_url=url,
            public_key="public",
            secret_key="private",
            transport=httpx.MockTransport(lambda request: pytest.fail("network")),
        )
    assert error.value.code == "langfuse_endpoint_invalid"


# ---------------------------------------------------------------- 读回


def remote_view(export: LangfuseExport) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """按Langfuse v4公开API的形状模拟已完整写入的远端状态。"""
    raw = json.loads(export.model_dump_json())
    experiments: dict[str, dict] = {}
    items: dict[str, list[dict]] = {}
    for span in spans_of(export):
        values = attrs(span)
        if "langfuse.experiment.id" not in values:
            continue  # 步骤Observation由v2 observations接口读回
        experiment = values["langfuse.experiment.id"]
        metadata = {
            key.removeprefix(METADATA): value
            for key, value in values.items()
            if key.startswith(METADATA)
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
        items.setdefault(experiment, []).append(
            {
                "id": span["spanId"],
                "traceId": span["traceId"],
                "experimentItemId": values["langfuse.experiment.item.id"],
                "experimentDatasetId": values["langfuse.experiment.dataset.id"],
                "input": values["langfuse.observation.input"],
                "output": values["langfuse.observation.output"],
                "metadata": metadata,
                "scores": scores,
            }
        )
        experiments[experiment] = {
            "id": experiment,
            "name": values["langfuse.experiment.name"],
            "datasetId": values["langfuse.experiment.dataset.id"],
            "itemCount": len(items[experiment]),
        }
    return experiments, items


def serve(experiments: dict[str, dict], items: dict[str, list[dict]]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path.endswith("/experiments"):
            data = [experiments[request.url.params["id"]]]
        else:
            data = items[request.url.params["experimentId"]]
        return httpx.Response(200, json={"data": data, "meta": {}})

    return handler


def verify(export: LangfuseExport, handler, timeout: float = 1):
    return read_back(
        export,
        base_url="http://127.0.0.1:3300/prefix",
        public_key="public",
        secret_key="private",
        timeout_seconds=timeout,
        transport=httpx.MockTransport(handler),
    )


def test_readback_verifies_every_experiment_with_cursor_pages() -> None:
    export = build_export(report())
    experiments, items = remote_view(export)
    paths = []
    base = serve(experiments, items)

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.params["fromStartTime"]
        if request.url.path.endswith("/experiment-items") and "cursor" not in request.url.params:
            return httpx.Response(200, json={"data": [], "meta": {"cursor": "page-two"}})
        return base(request)

    result = verify(export, handler)
    assert (result.experiments, result.verified_trials, result.verified_scores) == (
        2,
        4,
        len(export.scores),
    )
    assert len(paths) == 6
    assert all(path.startswith("/prefix/api/public/") for path in paths)


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
        "experiment_name",
    ],
)
def test_readback_does_not_accept_wrong_or_duplicate_evidence(mode: str) -> None:
    export = build_export(report())
    experiments, items = remote_view(export)
    experiment = next(iter(items))
    item = items[experiment][0]
    if mode == "duplicate_item":
        items[experiment].append(copy.deepcopy(item))
    elif mode == "trace":
        item["traceId"] = "a" * 32
    elif mode == "dataset":
        item["experimentDatasetId"] = "different-dataset"
    elif mode == "currency":
        item["metadata"]["cost_currency"] = "USD"
    elif mode == "integer":
        item["metadata"]["usage_input_tokens"] = "100.0"
    elif mode == "score_value":
        item["scores"][-1]["value"] = 999
    elif mode == "score_bool":
        item["scores"][-1]["value"] = True
    elif mode == "score_time":
        item["scores"][0]["timestamp"] = "2020-01-01T00:00:00Z"
    elif mode == "score_subject":
        item["scores"][0]["subject"]["id"] = "other-observation"
    elif mode == "duplicate_score":
        item["scores"].append(copy.deepcopy(item["scores"][0]))
    elif mode == "experiment_name":
        experiments[experiment]["name"] = "renamed"
    else:
        extra = copy.deepcopy(item["scores"][0])
        extra["id"] = "f" * 64
        item["scores"].append(extra)
    with pytest.raises(EvalkitError) as error:
        verify(export, serve(experiments, items))
    assert error.value.code == "langfuse_readback_mismatch"


def test_integer_metadata_may_come_back_as_canonical_strings() -> None:
    export = build_export(report())
    experiments, items = remote_view(export)
    for values in items.values():
        for item in values:
            item["metadata"] = {
                key: str(value) if isinstance(value, int) else value
                for key, value in item["metadata"].items()
            }
    assert verify(export, serve(experiments, items)).verified_trials == 4


def test_numeric_scores_tolerate_double_precision_round_trip() -> None:
    """E0c实测：Langfuse把36.345758读回为36.345758000000004，不能判为证据不符。"""
    export = build_export(report())
    experiments, items = remote_view(export)
    perturbed = 0
    for values in items.values():
        for item in values:
            for score in item["scores"]:
                if score["dataType"] == "NUMERIC" and score["value"]:
                    score["value"] = math.nextafter(score["value"], math.inf)
                    perturbed += 1
    assert perturbed
    assert verify(export, serve(experiments, items)).verified_scores == len(export.scores)


def test_pending_data_times_out_with_reads_only() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"data": [], "meta": {}})

    with pytest.raises(EvalkitError) as error:
        verify(build_export(report()), handler, timeout=0.01)
    assert error.value.code == "langfuse_readback_timeout"
    assert calls and set(calls) == {"GET"}


def test_readback_failure_does_not_expose_response_body() -> None:
    with pytest.raises(EvalkitError) as error:
        verify(build_export(report()), lambda _: httpx.Response(401, text="PRIVATE-READBACK-BODY"))
    assert "PRIVATE-" not in str(error.value)
    assert error.value.code == "langfuse_readback_rejected"


# ---------------------------------------------------------------- 命令行


def write_report(path: Path) -> Path:
    write_model(path, report(), 1024 * 1024)
    return path


def test_offline_export_never_reads_credentials_and_protects_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = cli.os.environ.get

    def guarded(key: str, default: Any = None) -> Any:
        if key.startswith("LANGFUSE_"):
            pytest.fail("credential read")
        return original(key, default)

    monkeypatch.setattr(cli.os.environ, "get", guarded)
    source = write_report(tmp_path / "report.json")
    before = source.read_bytes()
    output = tmp_path / "export.json"
    cli.main(["export", "--report", str(source), "--output", str(output)])
    assert json.loads(capsys.readouterr().out)["reason"] == "prepared_offline"
    assert output.stat().st_mode & 0o777 == 0o600
    for arguments in (
        ["--output", str(source)],
        ["--output", str(output), "--verify"],
        ["--output", str(output), "--allow-network", "--verify"],
    ):
        with pytest.raises(SystemExit):
            cli.main(["export", "--report", str(source), *arguments])
        assert json.loads(capsys.readouterr().out)["reason"] in {
            "output_conflicts_with_source",
            "langfuse_readback_arguments_invalid",
        }
    assert source.read_bytes() == before


def test_export_refuses_symlinked_output_and_non_private_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_report(tmp_path / "report.json")
    target = tmp_path / "target.json"
    target.write_text("untouched")
    (tmp_path / "link.json").symlink_to(target)
    with pytest.raises(SystemExit):
        cli.main(["export", "--report", str(source), "--output", str(tmp_path / "link.json")])
    assert json.loads(capsys.readouterr().out)["reason"] == "file_path_denied"
    assert target.read_text(encoding="utf-8") == "untouched"
    source.chmod(0o644)
    with pytest.raises(SystemExit):
        cli.main(["export", "--report", str(source), "--output", str(tmp_path / "e.json")])
    assert json.loads(capsys.readouterr().out)["reason"] == "file_invalid"


def test_cli_network_path_over_real_loopback_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[tuple[str, str | None, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            traces = self.path.endswith("/traces")
            result = (
                {}
                if traces
                else {
                    "successes": [{"id": e["id"], "status": 201} for e in body["batch"]],
                    "errors": [],
                }
            )
            content = json.dumps(result).encode()
            self.send_response(200 if traces else 207)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args: Any) -> None:
            pass

    source, output = write_report(tmp_path / "report.json"), tmp_path / "export.json"
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "private")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            cli.main(
                [
                    "export",
                    "--report",
                    str(source),
                    "--output",
                    str(output),
                    "--allow-network",
                    "--langfuse-base-url",
                    f"http://127.0.0.1:{server.server_port}/prefix",
                ]
            )
        finally:
            server.shutdown()
            worker.join(timeout=2)
    assert json.loads(capsys.readouterr().out)["reason"] == "submitted"
    assert requests[0][0] == "/prefix/api/public/otel/v1/traces"
    assert all(authorization == "Basic cHVibGljOnByaXZhdGU=" for _, authorization, _ in requests)
    export = json.loads(output.read_text(encoding="utf-8"))
    events = [event for _, _, body in requests[1:] for event in body["batch"]]
    assert {event["id"] for event in events} == {score["id"] for score in export["scores"]}


# ---------------------------------------------------------------- 逐步记录


def step_trace(source: EvalRunReport, slot: int = 0) -> StepTrace:
    begin = source.trials[slot].started_at
    assert begin is not None
    return StepTrace(
        plan_digest=source.plan_digest,
        slot=slot,
        steps=(
            Step(
                step_id="call-1",
                kind="generation",
                name="model",
                started_at=begin,
                completed_at=begin + timedelta(seconds=2),
                status="ok",
                model="qwen3.8-flash-next",
                usage=Usage(completeness="complete", input_tokens=50, output_tokens=5),
                input_text="PRIVATE-PROMPT",
                output_text="PRIVATE-REPLY",
            ),
            Step(
                step_id="tool-1",
                parent_step_id="call-1",
                kind="tool",
                name="read_file",
                started_at=begin + timedelta(seconds=3),
                completed_at=begin + timedelta(seconds=4),
                status="error",
                metadata={"outcome": "failed"},
            ),
        ),
    )


def test_steps_become_child_observations_without_content_by_default() -> None:
    source = report()
    export = build_export(source, (step_trace(source),))
    spans = spans_of(export)
    assert len(spans) == 6
    root, generation, tool = spans[0], spans[1], spans[2]
    assert generation["parentSpanId"] == root["spanId"]
    assert tool["parentSpanId"] == generation["spanId"]
    values = attrs(generation)
    assert values["langfuse.observation.type"] == "generation"
    assert values["langfuse.observation.model.name"] == "qwen3.8-flash-next"
    assert json.loads(values["langfuse.observation.usage_details"]) == {"input": 50, "output": 5}
    assert "PRIVATE-" not in export.model_dump_json()
    assert attrs(tool)[METADATA + "ref_step_outcome"] == "failed"
    with pytest.raises(ValueError, match="缺失试次"):
        build_export(source, (step_trace(source).model_copy(update={"slot": 3}),))


def test_full_content_policy_exports_step_text() -> None:
    source = report()
    plan = RunPlan.model_validate_json(
        source.plan.model_copy(update={"content_policy": "full"}).model_dump_json()
    )
    full = EvalRunReport(
        plan=plan,
        plan_digest=plan.digest,
        trials=source.trials,
        summary=source.summary,
        completed_at=source.completed_at,
    )
    trace = step_trace(full)
    body = build_export(full, (trace,)).model_dump_json()
    assert "PRIVATE-PROMPT" in body and "PRIVATE-REPLY" in body


def remote_steps(export: LangfuseExport) -> dict[str, list[dict]]:
    by_trace: dict[str, list[dict]] = {}
    for span in spans_of(export):
        values = attrs(span)
        remote = {
            "id": span["spanId"],
            "traceId": span["traceId"],
            "type": str(values["langfuse.observation.type"]).upper(),
            "name": span["name"],
            "parentObservationId": span.get("parentSpanId"),
        }
        if "langfuse.observation.model.name" in values:
            remote["model"] = values["langfuse.observation.model.name"]
        if "langfuse.observation.usage_details" in values:
            remote["usageDetails"] = json.loads(values["langfuse.observation.usage_details"])
        by_trace.setdefault(span["traceId"], []).append(remote)
    return by_trace


@pytest.mark.parametrize(
    "mode", ["complete", "pending", "wrong_parent", "extra", "resent", "resent_conflict"]
)
def test_readback_verifies_step_observations(mode: str) -> None:
    source = report()
    export = build_export(source, (step_trace(source),))
    experiments, items = remote_view(export)
    observations = remote_steps(export)
    trace = spans_of(export)[0]["traceId"]
    if mode == "pending":
        observations[trace] = observations[trace][:-1]
    elif mode == "wrong_parent":
        observations[trace][-1]["parentObservationId"] = "0" * 16
    elif mode == "extra":
        observations[trace].append({**observations[trace][-1], "id": "f" * 16})
    elif mode == "resent":
        observations[trace] += [dict(row) for row in observations[trace]]
    elif mode == "resent_conflict":
        observations[trace].append({**observations[trace][-1], "name": "other"})
    base = serve(experiments, items)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/observations"):
            assert request.url.params["fields"] == "core,basic,usage,model"
            data = observations[request.url.params["traceId"]]
            return httpx.Response(200, json={"data": data, "meta": {}})
        return base(request)

    if mode in ("complete", "resent"):
        result = verify(export, handler)
        assert (result.verified_trials, result.verified_steps) == (4, 2)
        return
    with pytest.raises(EvalkitError) as error:
        verify(export, handler, timeout=0.05)
    assert error.value.code == (
        "langfuse_readback_timeout" if mode == "pending" else "langfuse_readback_mismatch"
    )
