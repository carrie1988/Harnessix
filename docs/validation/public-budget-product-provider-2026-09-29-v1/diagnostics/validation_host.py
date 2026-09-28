"""默认产品stdio的有限Provider契约验收；不作为工程质量评分或独立用户Beta。"""

from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime
from uuid import UUID

BEFORE = 'def add(left, right):\n    """返回两个操作数之和。"""\n    return left - right\n'
AFTER = BEFORE.replace('left - right', 'left + right')
KEY_ENV = 'HARNESSIX_PRODUCT_ACCEPTANCE_KEY'


def require(value, code):
    """只发布固定错误标识，不输出原异常、路径或正文。"""
    if not value:
        raise RuntimeError(code)


def configuration(plan):
    from harnessix.product_config.contracts import (
        EnvironmentSecretSourceConfig, ModelCapabilities, ModelProfile,
        ProductConfigV2, ProviderDefinition, SecretReference,
    )
    secret = SecretReference(name='acceptance-provider', version='v1')
    return ProductConfigV2(
        active_profile='acceptance',
        secret_sources=(EnvironmentSecretSourceConfig(secret=secret, environment_variable=KEY_ENV),),
        providers=(ProviderDefinition(provider_id='acceptance', kind='openai_chat', base_url=plan['base_url'], credential=secret, output_token_parameter='max_tokens'),),
        profiles=(ModelProfile(profile_id='acceptance', provider_id='acceptance', model=plan['model'], capabilities=ModelCapabilities(parallel_tool_calls=False), max_output_tokens=plan['max_output_tokens'], max_attempts=1, timeout_seconds=45.0, io_timeout_seconds=30.0),),
    )


def fixture_provider(observe_request):
    """只测试验收控制器，明确不计入真实请求或质量成绩。"""
    from harnessix.agent.models import ToolResultContent
    from harnessix.models.scripted import ScriptedProvider
    from harnessix.models.contracts import ResponseStarted, ResponseCompleted, ToolCallCompleted
    from tests.agent.helpers import answer

    class Fixture(ScriptedProvider):
        async def stream(self, request, cancel):
            observe_request(request.step)
            if request.step == 1:
                events = (ResponseStarted(response_id='fixture-read'), ToolCallCompleted(call_id='fixture-read', tool='read_file', arguments={'path':'calculator.py'}), ResponseCompleted(finish_reason='tool_calls'))
            elif request.step == 2:
                result = next(x.content for x in reversed(request.history) if isinstance(x.content, ToolResultContent))
                require(result.outcome == 'succeeded' and result.output['digest_status'] == 'complete', 'fixture_read_failed')
                events = (ResponseStarted(response_id='fixture-patch'), ToolCallCompleted(call_id='fixture-patch', tool='apply_patch_batch', arguments={'files':[{'operation':'replace','path':'calculator.py','expected_sha256':result.output['content_sha256'],'content':AFTER,'mode':420}]}), ResponseCompleted(finish_reason='tool_calls'))
            else:
                events = answer('修改完成')
            for event in events:
                cancel.checkpoint()
                yield event
    return Fixture([])


async def child(args, plan):
    from harnessix.agent.cancellation import CancelToken
    from harnessix.models._provider_io import validate_key
    from harnessix.product_config import server
    from harnessix.product_config.runtime import build_provider_bundle, default_provider_factory
    from scripts.provider_verification_budget import VerificationBudgetLedger
    from scripts.provider_verification_guard import BailianVerificationBounds, GuardedVerificationProvider

    bounds = BailianVerificationBounds(datetime.fromisoformat(plan['valid_from']), datetime.fromisoformat(plan['valid_until']), plan['max_output_tokens'])
    bounds.checkpoint()
    calls = []
    request_facts = []

    def observe_request(step):
        require(len(calls) < plan['max_requests_per_case'], 'request_limit_reached')
        calls.append(step)
        (Path(args.case)/'request-count.json').write_text(json.dumps({'provider_invocations':len(calls)})+'\n')

    with ExitStack() as resources:
        ledger = None
        if args.network:
            ledger = resources.enter_context(VerificationBudgetLedger(Path(args.ledger), UUID(args.period)))
            ledger.require_available()
            result = subprocess.run(('/usr/bin/security','find-generic-password','-s',args.service,'-a',args.account,'-w'), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=15)
            require(result.returncode == 0, 'credential_unavailable')
            key = validate_key(result.stdout.decode('utf-8').rstrip('\n'), headers_env='OPENAI_CUSTOM_HEADERS')
        else:
            key = 'owned-fixture-secret-not-a-provider-credential'
        os.environ[KEY_ENV] = key
        stop = CancelToken()

        class OwnedGuard(GuardedVerificationProvider):
            """只增加验收请求计数与原官方Client回收，不改变Stream事件。"""
            async def stream(self, request, cancel):
                from harnessix.agent.usage import ModelAttemptStarted, ModelUsageObserved
                from harnessix.models.contracts import ResponseCompleted
                observe_request(request.step)
                facts = {'step':request.step,'attempts_started':0,'complete_response':False}
                try:
                    async for event in super().stream(request, cancel):
                        if isinstance(event, ModelAttemptStarted):
                            facts['attempts_started'] += 1
                        elif isinstance(event, ModelUsageObserved):
                            facts.update(input_tokens=event.usage.input_tokens,output_tokens=event.usage.output_tokens,usage_completeness=event.usage.completeness,actual_model=event.actual_model)
                        elif isinstance(event, ResponseCompleted):
                            facts['complete_response'] = True
                        yield event
                finally:
                    request_facts.append(facts)

            async def aclose(self):
                await self.provider.aclose()

        def factory(definition, profile, api_key):
            require(definition.base_url == plan['base_url'] and profile.model == plan['model'] and profile.max_attempts == 1, 'provider_scope_changed')
            if not args.network:
                return fixture_provider(observe_request)
            return OwnedGuard(default_provider_factory(definition, profile, api_key), ledger, bounds, stop)

        async def budgeted(*arguments, **kwargs):
            return await build_provider_bundle(*arguments, **kwargs, factory=factory)

        # 验收进程仅替换工厂装饰器；产品预检、Source、Tool、审批、协议和Store均保持原实现。
        server.build_provider_bundle = budgeted
        if not args.network:
            import traceback
            from harnessix.app_server.server import AgentProtocolServer
            original_dispatch = AgentProtocolServer._dispatch

            async def diagnosed_dispatch(instance, method, params):
                try:
                    return await original_dispatch(instance, method, params)
                except Exception as error:
                    frames = []
                    for frame, line in traceback.walk_tb(error.__traceback__):
                        source = Path(frame.f_code.co_filename)
                        try:
                            relative = str(source.relative_to(args.root))
                        except ValueError:
                            relative = '<external>'
                        frames.append({'source':relative,'function':frame.f_code.co_name,'line':line})
                    diagnostic = {'error_type':type(error).__name__,'frames':frames,'raw_exception_recorded':False}
                    (Path(args.case)/'dispatch-diagnostic.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2)+'\n')
                    raise

            AgentProtocolServer._dispatch = diagnosed_dispatch
        await server.run_product_stdio(config_path=Path(args.case)/'config.json', profile_id=None, workspace=Path(args.case)/'workspace', state_directory=Path(args.case)/'state', input_stream=sys.stdin.buffer, output_stream=sys.stdout.buffer, git_executable=args.git)
        (Path(args.case)/'child-summary.json').write_text(json.dumps({'model_requests':len(calls) if args.network else 0,'provider_invocations':len(calls),'budget_guard_used':args.network,'request_limit':plan['max_requests_per_case'],'request_facts':request_facts})+'\n')


def budget_snapshot(args, *, require_resolved=True):
    from scripts.provider_verification_budget import VerificationBudgetLedger
    from harnessix.agent.errors import KernelError
    ledger = VerificationBudgetLedger(Path(args.ledger), UUID(args.period))
    try:
        with ledger:
            if require_resolved:
                ledger.require_available()
    except KernelError as error:
        # 原Owner先验真后报告未决；只读取已验证快照，不修改或清除持久预留。
        if require_resolved or error.code != 'verification_budget_unresolved' or not ledger.data:
            raise
    return {'allocation_cny':ledger.period['allocation'],'known_estimated_cny':ledger.period['known_cost'],'reserved_cny':ledger.period['reserved_cost'],'request_count':len(ledger.period['requests']),'unresolved':sum(r['status'] in ('unknown','reserved') for r in ledger.period['requests']),'requests':[{'request_id':r['request_id'],'status':r['status'],'cost_estimate':r.get('cost_estimate'),'step':r.get('step'),'requested_model':r.get('requested_model')} for r in ledger.period['requests']]}


def make_case(root, name, plan, git):
    path = root/name
    path.mkdir(mode=0o700)
    workspace = path/'workspace';workspace.mkdir(mode=0o700)
    (workspace/'calculator.py').write_text(BEFORE)
    config = path/'config.json';config.write_text(configuration(plan).model_dump_json(indent=2)+'\n');config.chmod(0o600)
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run((git,'init','--quiet',str(workspace)), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return path


async def run_case(args, plan, case, cancel):
    from harnessix.sdk.agent_client import AgentClient
    from harnessix.sdk.subprocess import SubprocessAgentTransport
    from harnessix.protocol.contracts import ApprovalRespondParams, PublicApprovalDecision, PublicBudget

    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--root', args.root, '--plan', args.plan, '--case', str(case), '--git', args.git]
    if args.network:
        command += ['--network','--ledger',args.ledger,'--period',args.period,'--service',args.service,'--account',args.account]
    transport = SubprocessAgentTransport(command)
    client = AgentClient(transport)
    observed = {'case':case.name,'mode':'real_guarded_default_product' if args.network else 'fixture_controller_only','source_revision':plan['source_revision'],'quality_trials_counted':0,'commercial_release':False}
    stage='initialize'
    try:
        async with asyncio.timeout(180):
            await client.initialize()
            stage='create_thread'
            thread = await client.create_thread(str(case/'workspace'), request_id=case.name+'-thread')
            stage='start_turn'
            started = await client.start_turn(thread.thread_id, '修复 calculator.py 的 add：仅将 return left - right 改成 return left + right。先读取真实文件并使用读取返回的 content_sha256 作为写前条件，通过 apply_patch_batch 提交修改；不修改其他文件，不运行任何命令。', request_id=case.name+'-turn', budget=PublicBudget(max_steps=8,max_tokens=32000,timeout_seconds=120.0,max_output_chars=32000,max_tool_calls_per_step=1))
            approval = None
            tool_calls = {}
            successful_read = False
            stage='read_approval_events'
            while approval is None:
                replay = await client.replay_events(thread.thread_id, limit=256)
                for event in replay.events:
                    content = getattr(getattr(event.data,'item',None),'content',None)
                    if getattr(content,'kind',None)=='tool_call':
                        tool_calls[content.call_id] = content
                    if getattr(content,'kind',None)=='tool_result':
                        call = tool_calls.get(content.call_id)
                        if call is not None and call.tool=='read_file':
                            require(content.outcome=='succeeded' and isinstance(content.output,dict) and content.output.get('content_sha256')==hashlib.sha256(BEFORE.encode()).hexdigest(), 'trusted_read_mismatch')
                            successful_read = True
                    if getattr(content,'kind',None)=='approval_request':
                        approval = content
                        break
                current = await client.get_thread(thread.thread_id)
                if current.latest_turn is not None and current.latest_turn.status in ('completed','failed','interrupted'):
                    require(approval is not None,'no_approval_before_terminal')
                await asyncio.sleep(.1)
            require(successful_read, 'missing_trusted_read')
            require((case/'workspace/calculator.py').read_text()==BEFORE,'changed_before_approval')
            proposal = tool_calls.get(approval.call_id)
            require(proposal is not None and proposal.tool=='apply_patch_batch', 'unexpected_patch_tool')
            files = proposal.arguments.get('files')
            require(isinstance(files,list) and len(files)==1 and isinstance(files[0],dict), 'unexpected_patch_file_set')
            file = files[0]
            require(file.get('operation')=='replace' and file.get('path')=='calculator.py' and file.get('expected_sha256')==hashlib.sha256(BEFORE.encode()).hexdigest() and file.get('content')==AFTER and file.get('mode') in (None,420), 'unexpected_patch_scope')
            require(approval.approval_type=='patch_batch' and approval.diff_artifact is not None and approval.diff_artifact.complete,'approval_diff_incomplete')
            stage='read_diff'
            diff = await client.read_artifact(thread.thread_id,approval.diff_artifact.artifact_id,limit=200)
            require(diff.artifact==approval.diff_artifact and diff.offset==0 and diff.next_offset is None and 'calculator.py' in diff.text and 'return left + right' in diff.text,'unexpected_patch_proposal')
            observed.update(approval_seen=True,complete_diff=True,trusted_read_seen=True,exact_proposal_scope=True,unchanged_before_approval=True)
            if cancel:
                observed['provider_invocations_before_cancel']=json.loads((case/'request-count.json').read_text())['provider_invocations']
                stage='cancel_turn'
                result = await client.cancel_turn(thread.thread_id,started.turn_id,request_id=case.name+'-cancel')
                require(result.status=='interrupted','cancel_not_interrupted')
                require((case/'workspace/calculator.py').read_text()==BEFORE,'cancel_changed_fixture')
                observed.update(cancel_interrupted=True,fixture_unchanged=True)
            else:
                stage='respond_approval'
                await client.respond_approval(ApprovalRespondParams(request_id=case.name+'-approval',thread_id=thread.thread_id,turn_id=started.turn_id,approval_id=approval.approval_id,fingerprint=approval.request_fingerprint,decision=PublicApprovalDecision(outcome='approved',actor='受控产品契约验收')))
                while True:
                    current = await client.get_thread(thread.thread_id)
                    if current.latest_turn is not None and current.latest_turn.status in ('completed','failed','interrupted'):
                        require(current.latest_turn.status=='completed','approved_turn_not_completed')
                        break
                    await asyncio.sleep(.1)
                require((case/'workspace/calculator.py').read_text()==AFTER,'approved_effect_mismatch')
                observed.update(approved_exact_change=True,turn_completed=True)
            observed['result']='PASS'
    except Exception as error:
        observed.update(result='FAIL',failure_type=type(error).__name__,failure_stage=stage)
        code=getattr(error,'code',None)
        if isinstance(code,str) and len(code)<=128 and code.replace('_','').isalnum():
            observed['failure_code']=code
        if isinstance(error,RuntimeError) and str(error).replace('_','').isalnum():
            observed['failure_code']=str(error)
    finally:
        await client.close()
        observed['transport_closed']=transport.snapshot().state=='closed'
    if (case/'child-summary.json').is_file():
        observed['child']=json.loads((case/'child-summary.json').read_text())
    if observed.get('result')=='PASS':
        observed['only_fixture_path_present']=sorted(str(p.relative_to(case/'workspace')) for p in (case/'workspace').rglob('*') if p.is_file() and '.git' not in p.relative_to(case/'workspace').parts)==['calculator.py']
        if cancel:
            observed['no_new_provider_request_after_cancel']=observed.get('child',{}).get('provider_invocations')==observed.get('provider_invocations_before_cancel')
        if not observed['only_fixture_path_present'] or (cancel and not observed['no_new_provider_request_after_cancel']):
            observed.update(result='FAIL',failure_code='post_close_contract_mismatch')
    (case/'result.json').write_text(json.dumps(observed,ensure_ascii=False,indent=2)+'\n')
    return observed


async def driver(args, plan):
    root=Path(args.case)
    root.mkdir(mode=0o700)
    before=budget_snapshot(args) if args.network else None
    results=[]
    for name,cancel in [('sdk-read-approve-patch',False),('sdk-read-patch-cancel',True)]:
        case=make_case(root,name,plan,args.git)
        result=await run_case(args,plan,case,cancel)
        results.append(result)
        if result['result']!='PASS':
            break
    after=budget_snapshot(args,require_resolved=False) if args.network else None
    from harnessix.models.pricing import amount_units, format_amount
    requests = after['requests'][before['request_count']:] if args.network else []
    settlement = not args.network or (after['unresolved']==0 and all(r['status']=='completed' for r in requests) and len(requests)==sum(r.get('child',{}).get('model_requests',0) for r in results))
    facts = [f for r in results for f in r.get('child',{}).get('request_facts',[])]
    usage_complete = not args.network or (len(facts)==len(requests) and all(f.get('attempts_started')==1 and f.get('complete_response') is True and f.get('usage_completeness')=='complete' and f.get('actual_model')==plan['model'] for f in facts))
    report={'spec_version':'harnessix.product-provider-certification-result/v1','source_revision':plan['source_revision'],'cases':results,'budget_before':before,'budget_after':after,'task_pack_grader_unchanged':True,'quality_trials_counted':0,'commercial_release':False,'budget_settlement_complete':settlement if args.network else None,'usage_complete':usage_complete if args.network else None,'result':'PASS' if len(results)==2 and all(r['result']=='PASS' for r in results) and settlement and usage_complete else 'FAIL'}
    if args.network:
        report['new_requests'] = len(requests)
        report['new_estimated_cost_cny'] = format_amount(amount_units(after['known_estimated_cny'])-amount_units(before['known_estimated_cny']))
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--child',action='store_true');parser.add_argument('--network',action='store_true')
    for name in ('root','plan','case','git','ledger','period','service','account'):
        parser.add_argument('--'+name,required=name in ('root','plan','case','git'))
    args=parser.parse_args()
    sys.path[:0]=[str(Path(args.root)/'src'),args.root]
    logging.disable(logging.CRITICAL)
    plan=json.loads(Path(args.plan).read_text())
    require(subprocess.check_output(('git','-C',args.root,'rev-parse','HEAD'),text=True).strip()==plan['source_revision'],'source_revision_changed')
    require(not args.network or all(getattr(args,k) for k in ('ledger','period','service','account')),'network_scope_missing')
    if args.network:
        require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==plan['driver_sha256'], 'validation_driver_changed')
        require(not subprocess.check_output(('git','-C',args.root,'status','--porcelain','--untracked-files=no')), 'source_worktree_not_clean')
    asyncio.run(child(args,plan) if args.child else driver(args,plan))


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'result':'FAIL','failure_type':type(error).__name__}),file=sys.stderr)
        raise SystemExit(2) from None
