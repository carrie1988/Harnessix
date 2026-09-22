from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from scripts.soak_sample_file import SAMPLE_FILENAME, read_sample_file, write_sample_file
from scripts.soak_samples import SoakSample

RUN_ID = uuid4().hex
COUNTS = {"turn_local": 1, "rss_peak": 1}


def _samples() -> tuple[SoakSample, ...]:
    return (
        SoakSample(
            spec_version="harnessix.soak-sample/v1",
            run_id=RUN_ID,
            scenario_id="long_session",
            sample_index=1,
            phase="measure",
            metric="turn_local",
            value=100,
            unit="ns",
            clock="monotonic_ns",
        ),
        SoakSample(
            spec_version="harnessix.soak-sample/v1",
            run_id=RUN_ID,
            scenario_id="long_session",
            sample_index=2,
            phase="measure",
            metric="rss_peak",
            value=4096,
            unit="bytes",
            rss_source="getrusage",
            rss_raw_unit="KiB",
            rss_normalization="kib_times_1024",
            rss_raw_value=4,
            rss_bytes=4096,
        ),
    )


def test_file_round_trip_recomputes_statistics_and_refuses_overwrite(tmp_path) -> None:
    expected_hash, expected_statistics = write_sample_file(
        tmp_path,
        _samples(),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )

    loaded, actual_hash, actual_statistics = read_sample_file(
        tmp_path,
        expected_sha256=expected_hash,
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )

    assert loaded == _samples()
    assert actual_hash == expected_hash
    assert actual_statistics == expected_statistics
    assert actual_statistics["turn_local"].p95 == 100
    with pytest.raises(KernelError) as error:
        write_sample_file(
            tmp_path,
            _samples(),
            run_id=RUN_ID,
            scenario_id="long_session",
            expected_measured=COUNTS,
        )
    assert error.value.code == "soak_samples_write_failed"


def test_file_tampering_and_truncation_fail_closed(tmp_path) -> None:
    expected_hash, _ = write_sample_file(
        tmp_path,
        _samples(),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    target = tmp_path / SAMPLE_FILENAME
    original = target.read_bytes()
    for body in (
        original.rstrip(b"\n"),
        original.replace(b'"value":100', b'"value":101'),
        original.replace(b'"sample_index":2', b'"sample_index":3'),
        original.replace(b'"metric":"rss_peak"', b'"metric":"product_startup"'),
        original + b'{"prompt":"secret"}\n',
    ):
        target.write_bytes(body)
        with pytest.raises(KernelError) as error:
            read_sample_file(
                tmp_path,
                expected_sha256=expected_hash,
                run_id=RUN_ID,
                scenario_id="long_session",
                expected_measured=COUNTS,
            )
        assert error.value.code == "soak_samples_invalid"
    target.write_bytes(original)


def test_missing_file_and_wrong_run_identity_are_rejected(tmp_path) -> None:
    with pytest.raises(KernelError, match="Soak样本文件无效"):
        read_sample_file(
            tmp_path,
            expected_sha256="0" * 64,
            run_id=RUN_ID,
            scenario_id="long_session",
            expected_measured=COUNTS,
        )
    expected_hash, _ = write_sample_file(
        tmp_path,
        _samples(),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    with pytest.raises(KernelError, match="Soak样本文件无效"):
        read_sample_file(
            tmp_path,
            expected_sha256=expected_hash,
            run_id=uuid4().hex,
            scenario_id="long_session",
            expected_measured=COUNTS,
        )
