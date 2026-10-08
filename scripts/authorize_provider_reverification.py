"""可信预算管理入口：登记既有明确授权，不联网、不读取凭据、不释放旧预留。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.evals.cli_config import read_private_eval_config
from scripts.provider_reverification_binding import VerificationReverificationBinding
from scripts.provider_reverification_chain import VerificationCandidateBinding
from scripts.provider_reverification_plan import read_reverification_plan
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.run_engineering_provider_suite_budgeted import _SafeParser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _SafeParser(description=__doc__, allow_abbrev=False)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan")
    action.add_argument("--rebind-plan")
    action.add_argument("--append-binding-plan")
    parser.add_argument("--budget-ledger", type=Path, required=True)
    arguments = parser.parse_args(argv)
    reason = "verification_reverification_invalid"
    try:
        if arguments.append_binding_plan is not None:
            candidate = read_private_eval_config(
                arguments.append_binding_plan, VerificationCandidateBinding, max_bytes=64 * 1024
            )
            VerificationBudgetLedger.append_reverification_binding(
                arguments.budget_ledger, candidate
            )
            result = {"reason": "candidate_appended", "binding_id": str(candidate.binding_id)}
        elif arguments.rebind_plan is not None:
            binding = read_private_eval_config(
                arguments.rebind_plan, VerificationReverificationBinding, max_bytes=64 * 1024
            )
            VerificationBudgetLedger.rebind_reverification(arguments.budget_ledger, binding)
            result = {
                "reason": "rebound",
                "reverification_id": str(binding.reverification_id),
                "binding_id": str(binding.binding_id),
            }
        else:
            plan = read_reverification_plan(arguments.plan)
            VerificationBudgetLedger.authorize_reverification(arguments.budget_ledger, plan)
            result = {"reason": "authorized", "reverification_id": str(plan.reverification_id)}
        print(json.dumps(result))
        return
    except KernelError as error:
        if error.code in {
            "verification_budget_busy",
            "verification_budget_unavailable",
            "verification_budget_exhausted",
            "verification_budget_persist_failed",
        }:
            reason = error.code
    except Exception:
        pass
    print(json.dumps({"reason": reason}))
    raise SystemExit(1)


if __name__ == "__main__":
    main()
