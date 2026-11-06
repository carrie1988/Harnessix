"""Langfuse部署验收：写入一份合成运行报告并读回；重启后只读回同一导出，核对数据保留。

不调用模型或被测系统。凭据只从LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY环境变量读取。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from evalkit.files import read_model, write_model
from evalkit.langfuse import LangfuseExport, build_export, read_back, send

_GRADER = GraderSpec(grader_id="synthetic", version="1", pass_score="passed")
_MAX = 16 * 1024 * 1024


def synthetic_report(now: datetime) -> EvalRunReport:
    """两组别×两条目，含一个缺失槽位与一个已知人民币成本；全部为合成数据。"""
    plan = RunPlan(
        run_name=f"langfuse-smoke-{now:%Y%m%dT%H%M%SZ}",
        dataset=DatasetManifest(
            name="langfuse-smoke",
            version="1",
            items=tuple(
                DatasetItem(item_id=f"item-{i}", item_digest="sha256:" + str(i + 1) * 64)
                for i in range(2)
            ),
        ),
        arms=tuple(
            ArmSpec(
                arm_id=arm,
                system=arm,
                system_version="smoke",
                config_digest="sha256:" + "c" * 64,
                executor="import",
            )
            for arm in ("arm-a", "arm-b")
        ),
        attempts_per_item=1,
        graders=(_GRADER,),
        primary_grader=_GRADER.grader_id,
        created_at=now,
    )
    start = now + timedelta(seconds=1)
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
                scores=(Score(name="passed", value=slot == 0, grader_id=_GRADER.grader_id),),
                usage=Usage(completeness="complete", model_calls=1, input_tokens=10),
                cost=Cost(completeness="complete", currency="CNY", amount="0.01")
                if slot == 0
                else Cost(),
                started_at=start,
                completed_at=start + timedelta(seconds=2),
            )
        )
    ordered = tuple(trials)
    return EvalRunReport(
        plan=plan,
        plan_digest=plan.digest,
        trials=ordered,
        summary=summarize(plan, ordered),
        completed_at=start + timedelta(seconds=10),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("send", "verify"))
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:3300")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    credentials = {
        "base_url": args.base_url,
        "public_key": os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        "secret_key": os.environ.get("LANGFUSE_SECRET_KEY", ""),
    }
    directory = args.output_directory
    if args.action == "send":
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        report = synthetic_report(datetime.now(UTC).replace(microsecond=0))
        export = build_export(report)
        write_model(directory / "report.json", report, _MAX)
        write_model(directory / "export.json", export, _MAX)
        send(export, **credentials)
        receipt = directory / "readback.json"
    else:
        export = read_model(directory / "export.json", LangfuseExport, _MAX, require_private=True)
        receipt = directory / f"readback-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    result = read_back(export, timeout_seconds=args.timeout_seconds, **credentials)
    write_model(receipt, result, 65_536)
    print(json.dumps({"action": args.action, **result.model_dump(mode="json")}))


if __name__ == "__main__":
    main()
