from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from scripts.soak_samples import SoakSample, nearest_rank, validate_sample_series

RUN_ID = uuid4().hex


def _latency(index: int, *, phase: str = "measure", value: int = 1) -> SoakSample:
    return SoakSample.model_validate(
        {
            "spec_version": "harnessix.soak-sample/v1",
            "run_id": RUN_ID,
            "scenario_id": "long_session",
            "sample_index": index,
            "phase": phase,
            "metric": "turn_local",
            "value": value,
            "unit": "ns",
            "clock": "monotonic_ns",
        }
    )


def _rss(index: int, *, raw_unit: str = "KiB", raw_value: int = 4) -> SoakSample:
    return SoakSample.model_validate(
        {
            "spec_version": "harnessix.soak-sample/v1",
            "run_id": RUN_ID,
            "scenario_id": "long_session",
            "sample_index": index,
            "phase": "measure",
            "metric": "rss_peak",
            "value": raw_value * (1024 if raw_unit == "KiB" else 1),
            "unit": "bytes",
            "rss_source": "getrusage",
            "rss_raw_unit": raw_unit,
            "rss_normalization": "kib_times_1024" if raw_unit == "KiB" else "identity",
            "rss_raw_value": raw_value,
            "rss_bytes": raw_value * (1024 if raw_unit == "KiB" else 1),
        }
    )


def test_quantiles_are_recomputed_with_integer_nearest_rank() -> None:
    summary = nearest_rank(tuple(range(1, 101)))

    assert (summary.p50, summary.p95, summary.p99) == (50, 95, 99)
    assert nearest_rank((9,)).p99 == 9
    with pytest.raises(ValueError):
        nearest_rank((1, -1))
    with pytest.raises(ValueError):
        nearest_rank((1, True))


def test_sample_series_excludes_warmup_and_checks_frozen_counts() -> None:
    samples = (
        _latency(1, phase="warmup", value=900),
        _latency(2, value=10),
        _latency(3, value=20),
        _rss(4),
    )

    statistics = validate_sample_series(
        samples,
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured={"turn_local": 2, "rss_peak": 1},
    )

    assert statistics["turn_local"].p95 == 20
    assert statistics["rss_peak"].p50 == 4096
    with pytest.raises(ValueError, match="样本数"):
        validate_sample_series(
            samples,
            run_id=RUN_ID,
            scenario_id="long_session",
            expected_measured={"turn_local": 3, "rss_peak": 1},
        )
    with pytest.raises(ValueError, match="序号"):
        validate_sample_series(
            samples[:2] + samples[3:],
            run_id=RUN_ID,
            scenario_id="long_session",
            expected_measured={"turn_local": 1, "rss_peak": 1},
        )


def test_sample_rejects_unknown_fields_wrong_clock_and_wrong_scenario() -> None:
    valid = _latency(1).model_dump(mode="json")
    for changes in (
        {"spec_version": "harnessix.soak-sample/v2"},
        {"prompt": "敏感正文"},
        {"value": -1},
        {"value": float("nan")},
        {"sample_index": True},
        {"clock": None},
        {"scenario_id": "restart"},
        {"rss_bytes": 1},
    ):
        with pytest.raises(ValidationError):
            SoakSample.model_validate({**valid, **changes})
    with pytest.raises(ValidationError):
        SoakSample.model_validate(
            {key: value for key, value in valid.items() if key != "spec_version"}
        )


def test_rss_raw_value_and_normalization_must_match() -> None:
    assert _rss(1, raw_unit="KiB").value == 4096
    assert _rss(1, raw_unit="bytes").value == 4
    valid = _rss(1).model_dump(mode="json")
    for changes in (
        {"rss_bytes": 2048},
        {"rss_raw_unit": "bytes"},
        {"rss_raw_value": 0},
        {"rss_normalization": None},
        {"value": 0},
    ):
        with pytest.raises(ValidationError):
            SoakSample.model_validate({**valid, **changes})
