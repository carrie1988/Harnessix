"""复核原产品验收结果和认证历史；不构造模型请求或执行器。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree as ET


def require(value: object, code: str) -> None:
    """失败只输出固定标识，禁止记录原正文或凭据。"""
    if not value:
        raise AssertionError(code)


def load(path: Path) -> dict:
    return json.loads(path.read_text())


async def authenticated_metadata(case: Path, plan: dict) -> dict:
    """通过原Owner、Key绑定与Store读取原记录，只输出布尔事实。"""
    from harnessix.agent.models import ToolCallContent, ToolResultContent, TrustedActionApprovalRequestContent
    from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
    from harnessix.product_config.contracts import SecretReference
    from harnessix.product_config.session_key import open_product_session_binding
    from harnessix.product_config.state_owner import product_state_owner
    from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
    from harnessix.secrets.publication import SecretPublicationScope
    from harnessix.session.sqlite import SQLiteSessionStore

    require((case / 'state/sessions.db').is_file(), 'original_session_database_missing')
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource('acceptance-provider', 'v1', 'DIAGNOSTIC'),),
        environment={'DIAGNOSTIC': 'owned-read-only-metadata-not-a-provider-key'},
    )
    reads = 0
    patches = []
    approvals = 0
    with product_state_owner(case / 'state'), SecretPublicationScope(
        (SecretReference(name='acceptance-provider', version='v1'),), provider
    ) as scope:
        async with open_product_session_binding(case / 'state', scope) as binding:
            store = SQLiteSessionStore(case / 'state/sessions.db', publication=binding)
            threads, has_more = await store.list_thread_page(after=None, archived=None, limit=10)
            require(len(threads) == 1 and has_more is False, 'original_thread_scope_changed')
            for thread in threads:
                for turn in thread.turns:
                    calls = {}
                    for item in turn.items:
                        content = item.content
                        approvals += isinstance(content, TrustedActionApprovalRequestContent)
                        if isinstance(content, ToolCallContent):
                            calls[content.call_id] = content
                            if content.tool != 'apply_patch_batch':
                                continue
                            parsed = WorkspacePatchInput.model_validate_json(
                                json.dumps(content.arguments, ensure_ascii=False, allow_nan=False)
                            )
                            require(len(parsed.files) == 1, 'original_patch_scope_changed')
                            f = content.arguments['files'][0]
                            fact = {
                                'strict_original_decoder': True,
                                'read_precedes_patch': reads == 1,
                                'operation_replace': f.get('operation') == 'replace',
                                'only_allowed_path': f.get('path') == 'calculator.py',
                                'expected_digest_matches_original_before': f.get('expected_sha256') == plan['fixture']['before_sha256'],
                                'complete_body_matches_expected_after': hashlib.sha256(f.get('content', '').encode()).hexdigest() == plan['fixture']['after_sha256'],
                                'mode_explicitly_present': 'mode' in f,
                                'mode_nonnull_decimal_420': type(f.get('mode')) is int and f['mode'] == 420,
                            }
                            require(all(fact.values()), 'model_patch_contract_mismatch')
                            patches.append(fact)
                        elif isinstance(content, ToolResultContent):
                            call = calls.get(content.call_id)
                            if call is not None and call.tool == 'read_file':
                                require(content.outcome == 'succeeded', 'original_read_failed')
                                require(content.output['content_sha256'] == plan['fixture']['before_sha256'], 'original_read_digest_mismatch')
                                reads += 1
    require(reads == 1 and len(patches) == 1 and approvals == 1, 'original_history_shape_mismatch')
    return {
        'authenticated_original_store_read': True,
        'successful_original_reads': reads,
        'original_patch_calls': patches,
        'approval_items': approvals,
        'raw_values_recorded': False,
        'new_model_requests': 0,
        'effects_executed_by_verifier': 0,
    }


async def verify(base: Path, root: Path, ledger_path: Path, period: UUID) -> None:
    from scripts.provider_verification_budget import VerificationBudgetLedger

    plan = load(base / 'plan.json')
    offline = load(base / 'offline-v1/report.json')
    negative = load(base / 'offline-scope-negative-v1/report.json')
    negative_verification = load(base / 'scope-negative-verification.json')
    real = load(base / 'real-v1/report.json')
    checks = []
    require(hashlib.sha256((base / 'validation_host.py').read_bytes()).hexdigest() == plan['driver_sha256'], 'original_driver_sha_mismatch')
    require((base / 'validation_host.py').read_bytes() == (root / 'docs/validation/public-budget-product-provider-2026-09-29-v1/diagnostics/validation_host.py').read_bytes(), 'original_driver_bytes_changed')
    for report in [offline, negative, real]:
        require(report['source_revision'] == plan['source_revision'], 'result_source_mismatch')
        require(report['quality_trials_counted'] == 0 and report['commercial_release'] is False, 'scope_claim_mismatch')
    checks.append('original_driver_and_source_identity')
    require(offline['result'] == 'PASS' and len(offline['cases']) == 2, 'offline_cases_failed')
    require(all(c['child']['model_requests'] == 0 for c in offline['cases']), 'offline_model_request_present')
    checks.append('offline_approval_and_cancel')
    require(negative['result'] == 'FAIL' and len(negative['cases']) == 1, 'original_negative_result_changed')
    require(negative['cases'][0]['failure_code'] == 'unexpected_patch_file_set', 'negative_rejection_mismatch')
    require(negative_verification['result'] == 'PASS', 'negative_verification_failed')
    require(not (base / 'offline-scope-negative-v1/sdk-read-approve-patch/workspace/unapproved.py').exists(), 'negative_extra_file_present')
    require(hashlib.sha256((base / 'offline-scope-negative-v1/sdk-read-approve-patch/workspace/calculator.py').read_bytes()).hexdigest() == plan['fixture']['before_sha256'], 'negative_fixture_changed')
    checks.append('original_scope_negative_refused_without_effect')
    require(real['result'] == 'PASS' and len(real['cases']) == 2, 'real_cases_failed')
    require(real['usage_complete'] is True and real['budget_settlement_complete'] is True, 'real_usage_or_settlement_incomplete')
    checks.append('original_real_approval_and_cancel')
    metadata = {}
    for case in real['cases']:
        name = case['case']
        expected = plan['fixture']['after_sha256' if name == 'sdk-read-approve-patch' else 'before_sha256']
        require(hashlib.sha256((base / 'real-v1' / name / 'workspace/calculator.py').read_bytes()).hexdigest() == expected, 'final_fixture_digest_mismatch')
        files = [p.name for p in (base / 'real-v1' / name / 'workspace').iterdir() if p.name != '.git']
        require(files == ['calculator.py'], 'final_file_set_changed')
        metadata[name] = await authenticated_metadata(base / 'real-v1' / name, plan)
    checks.append('authenticated_original_model_mode_and_write_conditions')
    facts = [f for c in real['cases'] for f in c['child']['request_facts']]
    cost = Decimal(0)
    for f in facts:
        require(f['actual_model'] == plan['model'] and f['attempts_started'] == 1 and f['complete_response'] and f['usage_completeness'] == 'complete', 'request_attempt_or_usage_mismatch')
        for ceiling, input_rate, output_rate in plan['official_price_snapshot']['tiers']:
            if f['input_tokens'] <= ceiling:
                cost += (Decimal(f['input_tokens']) * input_rate + Decimal(f['output_tokens']) * output_rate) / Decimal(1000000)
                break
        else:
            require(False, 'request_input_outside_price_snapshot')
    require(cost == Decimal(real['new_estimated_cost_cny']), 'independent_usage_cost_mismatch')
    require(len(facts) == real['new_requests'] == real['budget_after']['request_count'] - real['budget_before']['request_count'], 'request_count_delta_mismatch')
    require(cost == Decimal(real['budget_after']['known_estimated_cny']) - Decimal(real['budget_before']['known_estimated_cny']), 'original_period_cost_delta_mismatch')
    require(str(period) == load(base / 'budget-before-real.json')['original_period'], 'original_period_identity_changed')
    ledger = VerificationBudgetLedger(ledger_path, period)
    with ledger:
        ledger.require_available()
        require(ledger.period['allocation'] == '70.00', 'original_allocation_changed')
        require(ledger.period['known_cost'] == real['budget_after']['known_estimated_cny'], 'current_original_period_cost_changed')
        require(ledger.period['reserved_cost'] == '0' and all(r['status'] not in ['unknown', 'reserved'] for r in ledger.period['requests']), 'original_unresolved_reservation_present')
    checks.append('complete_usage_recalculation_and_original_budget_owner')
    report = {
        'spec_version': 'harnessix.product-provider-independent-verification/v1',
        'source_revision': plan['source_revision'], 'product_code_revision': plan['product_code_revision'],
        'observed_at': datetime.now(timezone.utc).isoformat(), 'result': 'PASS',
        'checks': checks, 'original_driver_unchanged': True, 'negative_original_fail_preserved': True,
        'request_count': len(facts), 'input_tokens': sum(f['input_tokens'] for f in facts),
        'output_tokens': sum(f['output_tokens'] for f in facts), 'independently_estimated_cny': str(cost),
        'original_period': str(period), 'budget_reset': False,
        'original_period_known_estimated_cny': real['budget_after']['known_estimated_cny'],
        'unresolved': 0, 'authentication_metadata': metadata,
        'verifier_model_requests': 0, 'quality_trials_counted': 0, 'commercial_release': False,
    }
    (base / 'independent-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    suite = ET.Element('testsuite', name='product-provider-independent-verification', tests=str(len(checks)), failures='0', errors='0', skipped='0')
    for check in checks:
        ET.SubElement(suite, 'testcase', classname='source_c8033e0', name=check)
    ET.ElementTree(suite).write(base / 'independent-verification.xml', encoding='utf-8', xml_declaration=True)
    print(json.dumps({k: v for k, v in report.items() if k != 'authentication_metadata'}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--period', type=UUID, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.root / 'src'), str(args.root)]
    try:
        asyncio.run(verify(args.base, args.root, args.ledger, args.period))
    except Exception as error:
        print(json.dumps({'result': 'FAIL', 'error_type': type(error).__name__, 'raw_error_recorded': False}))
        raise SystemExit(1) from None
