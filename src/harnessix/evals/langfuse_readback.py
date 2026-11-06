"""有界读取Langfuse v4实验、根Observation及评分；不把HTTP接收当作验收通过。"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from harnessix.agent.errors import KernelError
from harnessix.evals.langfuse_contracts import LangfuseExport, LangfuseReadbackReport
from harnessix.evals.langfuse_transport import _client


def _mismatch() -> KernelError:
    return KernelError("langfuse_readback_mismatch", "Langfuse读回与原证据身份、评分或元数据不一致")


def _get(
    client: httpx.Client, path: str, params: dict[str, Any], deadline: float
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise KernelError("langfuse_readback_timeout", "Langfuse读回超时；没有重跑Agent")
    try:
        with client.stream("GET", path, params=params, timeout=min(10.0, remaining)) as response:
            if response.status_code != 200:
                raise KernelError("langfuse_readback_rejected", "Langfuse拒绝读回请求")
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise KernelError("langfuse_readback_timeout", "Langfuse读回超出预算")
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError
            result = json.loads(body)
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("data"), list)
                or not isinstance(result.get("meta"), dict)
            ):
                raise ValueError
            return result
    except (httpx.HTTPError, ValueError, RecursionError):
        raise KernelError("langfuse_readback_invalid", "Langfuse读回失败或响应格式无效") from None


def _pages(
    client: httpx.Client, path: str, params: dict[str, Any], deadline: float
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    params = {**params, "limit": 100}
    for _ in range(100):
        page = _get(client, path, params, deadline)
        if any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            for item in page["data"]
        ):
            raise _mismatch()
        result.extend(page["data"])
        if len(result) > 1000:
            raise _mismatch()
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor:
            return result
        if not isinstance(cursor, str) or cursor in seen:
            raise _mismatch()
        seen.add(cursor)
        params["cursor"] = cursor
    raise _mismatch()


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for item in span["attributes"]:
        value = item["value"]
        result[item["key"]] = (
            int(value["intValue"]) if "intValue" in value else next(iter(value.values()))
        )
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _metadata_scalar_matches(expected: Any, actual: Any) -> bool:
    if type(expected) is bool:
        return (type(actual) is bool and actual == expected) or (
            type(actual) is str and actual == json.dumps(expected)
        )
    if type(expected) is int:
        return (type(actual) is int and actual == expected) or (
            type(actual) is str and actual == str(expected)
        )
    return bool(actual == expected)


def _verify_metadata(attrs: dict[str, Any], metadata: Any) -> None:
    if not isinstance(metadata, dict):
        raise _mismatch()
    for key, value in attrs.items():
        prefix = "langfuse.observation.metadata."
        if key.startswith(prefix) and len(str(value)) <= 200:
            actual_value = metadata.get(key.removeprefix(prefix))
            # v4可按JSON规范字符串返回整数及布尔值；不接受宽松真值转换。
            if not _metadata_scalar_matches(value, actual_value):
                raise _mismatch()


def _verify_score(score: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("name", "dataType", "value", "environment"):
        if score.get(key) != source[key]:
            raise _mismatch()
    if source["dataType"] == "NUMERIC" and type(score.get("value")) not in (int, float):
        raise _mismatch()
    subject = score.get("subject")
    expected = {"kind": "observation", "id": source["observationId"], "traceId": source["traceId"]}
    if not isinstance(subject, dict) or any(
        subject.get(key) != value for key, value in expected.items()
    ):
        raise _mismatch()
    try:
        delta = datetime.fromisoformat(score["timestamp"]) - datetime.fromisoformat(
            source["timestamp"]
        )
    except (KeyError, TypeError, ValueError):
        raise _mismatch() from None
    if abs(delta.total_seconds()) > 0.001:
        raise _mismatch()


def _verify_scores(remote: Any, scores: list[dict[str, Any]]) -> bool:
    if not isinstance(remote, list) or any(
        not isinstance(score, dict)
        or not isinstance(score.get("id"), str)
        or not isinstance(score.get("name"), str)
        for score in remote
    ):
        raise _mismatch()
    names = {score["name"] for score in scores}
    actual = [
        score for score in remote if score.get("source") == "API" and score.get("name") in names
    ]
    expected = {score["id"]: score for score in scores}
    if len({score.get("id") for score in actual}) != len(actual) or any(
        score.get("id") not in expected for score in actual
    ):
        raise _mismatch()
    for score in actual:
        _verify_score(score, expected[score["id"]])
    return len(actual) == len(scores)


def _verify_item(item: dict[str, Any], span: dict[str, Any], scores: list[dict[str, Any]]) -> bool:
    attrs = _attributes(span)
    if (
        item.get("traceId") != span["traceId"]
        or item.get("experimentItemId") != attrs["langfuse.experiment.item.id"]
    ):
        raise _mismatch()
    if item.get("experimentDatasetId") != attrs["langfuse.experiment.dataset.id"]:
        raise _mismatch()
    for name in ("input", "output"):
        if _json_value(item.get(name)) != _json_value(attrs[f"langfuse.observation.{name}"]):
            raise _mismatch()
    _verify_metadata(attrs, item.get("metadata"))
    return _verify_scores(item.get("scores"), scores)


def _snapshot_complete(
    experiments: list[dict[str, Any]],
    items: list[dict[str, Any]],
    expected: dict[str, Any],
    scores: list[dict[str, Any]],
) -> bool:
    if len(experiments) > 1 or len({item["id"] for item in items}) != len(items):
        raise _mismatch()
    attrs = _attributes(next(iter(expected.values())))
    if experiments and (
        experiments[0].get("datasetId") != attrs["langfuse.experiment.dataset.id"]
        or experiments[0].get("name") != attrs["langfuse.experiment.name"]
    ):
        raise _mismatch()
    complete = len(items) == len(expected) and len(experiments) == 1
    for item in items:
        if item["id"] not in expected:
            raise _mismatch()
        item_scores = [score for score in scores if score["observationId"] == item["id"]]
        complete = _verify_item(item, expected[item["id"]], item_scores) and complete
    return complete and experiments[0].get("itemCount") == len(expected)


def verify_langfuse_export(
    export: LangfuseExport,
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    timeout_seconds: float = 120,
    transport: httpx.BaseTransport | None = None,
) -> LangfuseReadbackReport:
    """显式网络授权后轮询最终事实，核对全部槽位、评分、币种与身份，不重发或重跑。

    使用v4公开实验API，支持游标和空页；拒绝重定向、重复项、错误绑定及不一致数值。
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise KernelError("langfuse_readback_budget_invalid", "Langfuse读回预算必须在0到600秒之间")
    raw = json.loads(export.model_dump_json())
    spans = raw["resource_spans"][0]["scopeSpans"][0]["spans"]
    expected = {span["spanId"]: span for span in spans}
    attrs = _attributes(spans[0])
    experiment_id, dataset_id = (
        attrs["langfuse.experiment.id"],
        attrs["langfuse.experiment.dataset.id"],
    )
    start = (
        datetime(1970, 1, 1, tzinfo=UTC)
        + timedelta(microseconds=min(int(span["startTimeUnixNano"]) for span in spans) // 1000)
        - timedelta(seconds=1)
    )
    query = {"fromStartTime": start.isoformat()}
    deadline = time.monotonic() + timeout_seconds
    with _client(base_url, public_key, secret_key, transport) as client:
        while True:
            experiments = _pages(
                client, "api/public/experiments", {**query, "id": experiment_id}, deadline
            )
            items = _pages(
                client,
                "api/public/experiment-items",
                {
                    **query,
                    "experimentId": experiment_id,
                    "fields": "dataset,metadata,io,scores",
                    "scoreLimit": 50,
                },
                deadline,
            )
            if _snapshot_complete(experiments, items, expected, raw["scores"]):
                return LangfuseReadbackReport(
                    source_report_sha256=export.source_report_sha256,
                    experiment_id=experiment_id,
                    dataset_id=dataset_id,
                    verified_trials=len(items),
                    verified_scores=len(raw["scores"]),
                    verified_at=datetime.now(UTC),
                )
            if time.monotonic() >= deadline:
                raise KernelError("langfuse_readback_timeout", "Langfuse读回未完整；本地证据已保留")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
