"""有界读取Langfuse v4实验、根Observation及评分并逐项核对；HTTP接收不等于验收通过。"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from evalkit.errors import EvalkitError
from evalkit.langfuse.contracts import LangfuseExport, LangfuseReadback
from evalkit.langfuse.transport import client

_MAX_PAGES = 101
_MAX_ITEMS_PER_EXPERIMENT = 10_000
_SCORE_LIMIT = 50
_METADATA = "langfuse.observation.metadata."


def _mismatch() -> EvalkitError:
    return EvalkitError("langfuse_readback_mismatch", "Langfuse读回与原证据身份、评分或元数据不符")


def _get(http: httpx.Client, path: str, params: dict[str, Any], deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EvalkitError("langfuse_readback_timeout", "Langfuse读回超时；没有重跑被测系统")
    try:
        with http.stream("GET", path, params=params, timeout=min(10.0, remaining)) as response:
            if response.status_code != 200:
                raise EvalkitError("langfuse_readback_rejected", "Langfuse拒绝读回请求")
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise EvalkitError("langfuse_readback_timeout", "Langfuse读回超出预算")
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
        raise EvalkitError("langfuse_readback_invalid", "Langfuse读回失败或响应格式无效") from None


def _pages(
    http: httpx.Client, path: str, params: dict[str, Any], deadline: float
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    params = {**params, "limit": 100}
    for _ in range(_MAX_PAGES):
        page = _get(http, path, params, deadline)
        if any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            for item in page["data"]
        ):
            raise _mismatch()
        result.extend(page["data"])
        if len(result) > _MAX_ITEMS_PER_EXPERIMENT:
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
        if key.startswith(_METADATA) and len(str(value)) <= 200:
            # v4可按JSON规范字符串返回整数及布尔值；不接受宽松真值转换。
            if not _metadata_scalar_matches(value, metadata.get(key.removeprefix(_METADATA))):
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
        or item.get("experimentDatasetId") != attrs["langfuse.experiment.dataset.id"]
    ):
        raise _mismatch()
    for name in ("input", "output"):
        if _json_value(item.get(name)) != _json_value(attrs[f"langfuse.observation.{name}"]):
            raise _mismatch()
    _verify_metadata(attrs, item.get("metadata"))
    return _verify_scores(item.get("scores"), scores)


def _experiment_complete(
    experiments: list[dict[str, Any]],
    items: list[dict[str, Any]],
    expected: dict[str, dict[str, Any]],
    scores: dict[str, list[dict[str, Any]]],
) -> bool:
    """单个实验：远端多出、重复或错绑任何一项都立即失败；不足则继续等待。"""
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
        complete = _verify_item(item, expected[item["id"]], scores[item["id"]]) and complete
    return complete and experiments[0].get("itemCount") == len(expected)


def read_back(
    export: LangfuseExport,
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    timeout_seconds: float = 120,
    transport: httpx.BaseTransport | None = None,
) -> LangfuseReadback:
    """轮询v4公开Experiments与Experiment Items API，核对全部实验、槽位和评分；不重发、不重跑。"""
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise EvalkitError("langfuse_readback_budget_invalid", "Langfuse读回预算必须在0到600秒之间")
    raw = json.loads(export.model_dump_json())
    spans = raw["resource_spans"][0]["scopeSpans"][0]["spans"]
    by_experiment: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for span in spans:
        by_experiment[_attributes(span)["langfuse.experiment.id"]][span["spanId"]] = span
    scores: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in raw["scores"]:
        scores[score["observationId"]].append(score)
    if any(len(values) > _SCORE_LIMIT for values in scores.values()):
        raise EvalkitError("langfuse_readback_unsupported", "单个试次的分数超过读回上限")
    start = (
        datetime(1970, 1, 1, tzinfo=UTC)
        + timedelta(microseconds=min(int(span["startTimeUnixNano"]) for span in spans) // 1000)
        - timedelta(seconds=1)
    )
    query = {"fromStartTime": start.isoformat()}
    deadline = time.monotonic() + timeout_seconds
    with client(base_url, public_key, secret_key, transport) as http:
        while True:
            complete = True
            for experiment_id, expected in by_experiment.items():
                experiments = _pages(
                    http, "api/public/experiments", {**query, "id": experiment_id}, deadline
                )
                items = _pages(
                    http,
                    "api/public/experiment-items",
                    {
                        **query,
                        "experimentId": experiment_id,
                        "fields": "dataset,metadata,io,scores",
                        "scoreLimit": _SCORE_LIMIT,
                    },
                    deadline,
                )
                complete = _experiment_complete(experiments, items, expected, scores) and complete
            if complete:
                return LangfuseReadback(
                    source_report_digest=export.source_report_digest,
                    experiments=len(by_experiment),
                    verified_trials=len(spans),
                    verified_scores=len(raw["scores"]),
                    verified_at=datetime.now(UTC),
                )
            if time.monotonic() >= deadline:
                raise EvalkitError("langfuse_readback_timeout", "Langfuse读回不完整；本地证据保留")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
