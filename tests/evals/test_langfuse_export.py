"""用真实Suite合同验证只读投影、网络边界和可重复补发。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import cast
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue

from harnessix import cli
from harnessix.agent.errors import KernelError
from harnessix.evals import export_cli
from harnessix.evals.campaign_contracts import summarize_campaign_trials
from harnessix.evals.langfuse_projection import build_langfuse_suite_export
from harnessix.evals.langfuse_transport import send_langfuse_suite_export
from harnessix.evals.report import (
    read_eval_suite_report,
    write_eval_suite_report,
    write_langfuse_suite_export,
)
from harnessix.evals.suite import build_coding_eval_suite_report
from harnessix.evals.suite_contracts import CodingEvalSuiteReport, summarize_suite_cases
from harnessix.models.pricing import content_digest
from tests.evals.test_suite import _suite


def suite_report(*, failed: bool = False) -> CodingEvalSuiteReport:
    plan, cases = _suite(profile_evidence=not failed)
    return build_coding_eval_suite_report(plan, cases)


def spans_from(export) -> list[dict[str, JsonValue]]:
    scope = export.resource_spans[0]["scopeSpans"][0]
    return scope["spans"]


def attributes(span: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        item["key"]: next(iter(item["value"].values()))
        for item in cast(list[dict], span["attributes"])
    }


def test_projection_preserves_every_trial_and_original_summary() -> None:
    report = suite_report(failed=True)
    export = build_langfuse_suite_export(report)
    spans = spans_from(export)
    assert len(spans) == report.summary.scheduled_trials == 10
    assert export.summary == report.summary
    assert len({span["traceId"] for span in spans}) == 10
    assert sum(score["value"] for score in export.scores if score["name"] == "task_passed") == 0
    assert {score["traceId"] for score in export.scores} == {span["traceId"] for span in spans}
    assert all(attributes(span)["langfuse.observation.type"] == "agent" for span in spans)
    assert all("parentSpanId" not in span for span in spans)
    # Suite只有聚合用量，不能伪造逐调用generation或再次拆分/累计Token。
    assert "gen_ai.usage" not in export.model_dump_json()
    assert all(int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"]) for span in spans)


def test_projection_is_stable_and_pairs_items_across_experiments() -> None:
    report = suite_report()
    before = report.model_dump_json()
    first = build_langfuse_suite_export(report)
    assert first.model_dump_json() == build_langfuse_suite_export(report).model_dump_json()
    assert report.model_dump_json() == before
    assert {score["timestamp"] for score in first.scores} == {
        trial.completed_at.isoformat() for case in report.cases for trial in case.campaign.trials
    }
    plan = report.plan.model_copy(update={"suite_id": uuid4()})
    changed = report.model_copy(update={"plan": plan, "plan_fingerprint": plan.fingerprint})
    second = build_langfuse_suite_export(changed)
    paired_keys = ("langfuse.experiment.dataset.id", "langfuse.experiment.item.id")
    for before_span, after_span in zip(spans_from(first), spans_from(second), strict=True):
        assert before_span["traceId"] != after_span["traceId"]
        assert all(
            attributes(before_span)[key] == attributes(after_span)[key] for key in paired_keys
        )


def test_unknown_cost_is_missing() -> None:
    report = suite_report()
    cases = []
    for case in report.cases:
        trials = tuple(
            trial.model_copy(
                update={
                    "cost_completeness": "unknown",
                    "known_cost_currency": None,
                    "known_cost_amount": None,
                }
            )
            for trial in case.campaign.trials
        )
        campaign = case.campaign.model_copy(
            update={"trials": trials, "summary": summarize_campaign_trials(trials, "USD")}
        )
        cases.append(
            case.model_copy(
                update={
                    "campaign": campaign,
                    "campaign_report_fingerprint": content_digest(campaign),
                }
            )
        )
    changed = report.model_copy(
        update={"cases": tuple(cases), "summary": summarize_suite_cases(report.plan, tuple(cases))}
    )
    export = build_langfuse_suite_export(changed)
    assert export.summary.cost_completeness == "unknown"
    assert not any(str(score["name"]).startswith("known_cost_") for score in export.scores)
    assert "cost_details" not in export.model_dump_json()


def test_cny_is_preserved_without_usd_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.evals import test_suite

    price = test_suite.price().model_copy(update={"currency": "CNY"})
    monkeypatch.setattr(test_suite, "price", lambda: price)
    export = build_langfuse_suite_export(suite_report())
    assert export.summary.known_cost_currency == "CNY"
    names = {score["name"] for score in export.scores}
    assert "known_cost_CNY" in names
    assert "known_cost_USD" not in names
    assert "cost_details" not in export.model_dump_json()


def test_projection_uses_field_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.evals import test_suite

    environment = test_suite.campaign_environment().model_copy(
        update={"isolation": "PRIVATE-DIAGNOSTIC-MUST-NOT-EXPORT"}
    )
    monkeypatch.setattr(test_suite, "campaign_environment", lambda: environment)
    export = build_langfuse_suite_export(suite_report())
    body = export.model_dump_json()
    assert "PRIVATE-DIAGNOSTIC" not in body
    assert "https://github.com" not in body
    assert "baseline_tree" not in body


def test_offline_cli_never_reads_credentials_or_sends_and_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source, output = tmp_path / "suite.json", tmp_path / "export.json"
    write_eval_suite_report(source, suite_report())
    before = source.read_bytes()
    original_get = export_cli.os.environ.get

    def guarded_get(key: str, default=None):
        if key.startswith("LANGFUSE_"):
            raise AssertionError("offline credential read")
        return original_get(key, default)

    monkeypatch.setattr(export_cli.os.environ, "get", guarded_get)
    monkeypatch.setattr(
        export_cli,
        "send_langfuse_suite_export",
        lambda *args, **kwargs: pytest.fail("offline network call"),
    )
    cli.main(["coding-eval-export", "--suite-report", str(source), "--output", str(output)])
    assert json.loads(capsys.readouterr().out)["reason"] == "prepared_offline"
    assert output.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == before
    assert read_eval_suite_report(source).summary.scheduled_trials == 10


def test_cli_cannot_overwrite_source_or_follow_output_symlink(tmp_path: Path, capsys) -> None:
    source = tmp_path / "suite.json"
    write_eval_suite_report(source, suite_report())
    before = source.read_bytes()
    with pytest.raises(SystemExit):
        export_cli.main(["--suite-report", str(source), "--output", str(source)])
    assert json.loads(capsys.readouterr().out)["reason"] == "langfuse_export_source_conflict"
    target = tmp_path / "other.json"
    target.write_text("untouched")
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)
    with pytest.raises(KernelError, match="不能是符号链接"):
        write_langfuse_suite_export(linked, build_langfuse_suite_export(suite_report()))
    assert target.read_text() == "untouched"
    assert source.read_bytes() == before


def test_transport_sends_otlp_and_timestamped_score_batches_with_stable_ids() -> None:
    export = build_langfuse_suite_export(suite_report())
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        assert request.headers["authorization"].startswith("Basic ")
        assert request.headers["x-langfuse-ingestion-version"] == "4"
        if request.url.path.endswith("/traces"):
            return httpx.Response(200, json={})
        return httpx.Response(
            207,
            json={
                "successes": [{"id": event["id"], "status": 201} for event in body["batch"]],
                "errors": [],
            },
        )

    for _ in range(2):
        send_langfuse_suite_export(
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
    assert all(event["timestamp"] == export.scores[0]["timestamp"] for event in events)
    assert all("timestamp" not in event["body"] for event in events)


def test_cli_network_path_over_real_loopback_http(tmp_path: Path, monkeypatch, capsys) -> None:
    requests: list[tuple[str, str | None, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            result = (
                {}
                if self.path.endswith("/traces")
                else {
                    "successes": [{"id": event["id"], "status": 201} for event in body["batch"]],
                    "errors": [],
                }
            )
            content = json.dumps(result).encode()
            self.send_response(200 if self.path.endswith("/traces") else 207)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    source, output = tmp_path / "suite.json", tmp_path / "export.json"
    write_eval_suite_report(source, suite_report())
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "private")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            cli.main(
                [
                    "coding-eval-export",
                    "--suite-report",
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
    export = json.loads(output.read_text())
    events = [event for _, _, body in requests[1:] for event in body["batch"]]
    assert len(events) == len(export["scores"])
    assert {event["id"] for event in events} == {score["id"] for score in export["scores"]}


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

    with pytest.raises(KernelError) as failure:
        send_langfuse_suite_export(
            build_langfuse_suite_export(suite_report()),
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
    with pytest.raises(KernelError):
        send_langfuse_suite_export(
            build_langfuse_suite_export(suite_report()),
            base_url=url,
            public_key="public",
            secret_key="private",
            transport=httpx.MockTransport(lambda request: pytest.fail("network")),
        )
