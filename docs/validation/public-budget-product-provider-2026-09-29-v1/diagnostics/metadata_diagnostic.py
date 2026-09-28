"""原认证Store的低敏只读归因；不记录调用正文，不执行Patch或Provider。"""
from __future__ import annotations
import argparse,asyncio,copy,hashlib,json,sys
from pathlib import Path

async def diagnose(root: Path, output: Path):
    from pydantic import ValidationError
    from harnessix.agent.models import ToolCallContent,ToolResultContent
    from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
    from harnessix.product_config.session_key import open_product_session_binding
    from harnessix.product_config.state_owner import product_state_owner
    from harnessix.product_config.contracts import SecretReference
    from harnessix.secrets.provider import EnvironmentSecretProvider,EnvironmentSecretSource
    from harnessix.secrets.publication import SecretPublicationScope
    from harnessix.session.sqlite import SQLiteSessionStore
    fixture=(root/'workspace/calculator.py').read_bytes()
    expected=hashlib.sha256(fixture).hexdigest()
    after=fixture.replace(b'left - right',b'left + right').decode()
    provider=EnvironmentSecretProvider((EnvironmentSecretSource('acceptance-provider','v1','FIXTURE'),),environment={'FIXTURE':'read-only-owned-diagnostic-not-a-provider-key'})
    with product_state_owner(root/'state'),SecretPublicationScope((SecretReference(name='acceptance-provider',version='v1'),),provider) as scope:
        async with open_product_session_binding(root/'state',scope) as binding:
            store=SQLiteSessionStore(root/'state/sessions.db',publication=binding)
            threads,_=await store.list_thread_page(after=None,archived=None,limit=10)
            rows=[];patches=[];approvals=0
            for thread in threads:
                for turn in thread.turns:
                    calls={}
                    for item in turn.items:
                        c=item.content
                        approvals += getattr(c,'kind',None)=='approval_request'
                        if isinstance(c,ToolCallContent):
                            calls[c.call_id]=c
                            rows.append({'kind':'tool_call','tool':c.tool,'path_matches_fixture':c.arguments.get('path')=='calculator.py','has_files_argument':'files' in c.arguments})
                            if c.tool!='apply_patch_batch':continue
                            files=c.arguments.get('files')
                            p={'file_count':len(files) if isinstance(files,list) else None,'mode_present':[('mode' in f) for f in files if isinstance(f,dict)] if isinstance(files,list) else [],'mode_null':[f.get('mode') is None for f in files if isinstance(f,dict)] if isinstance(files,list) else [],'digest_matches_fixture':[f.get('expected_sha256')==expected for f in files if isinstance(f,dict)] if isinstance(files,list) else [],'content_matches_expected_fixture':[f.get('content')==after for f in files if isinstance(f,dict)] if isinstance(files,list) else []}
                            try:
                                WorkspacePatchInput.model_validate_json(json.dumps(c.arguments,ensure_ascii=False,allow_nan=False));p['contract_result']='PASS'
                            except ValidationError as error:
                                p['contract_result']='FAIL'
                                p['errors']=[{'location':[loc if isinstance(loc,int) or loc in {'files','operation','path','expected_sha256','content','mode'} else '<unrecognized_field>' for loc in e['loc']],'type':e['type']} for e in error.errors(include_input=False,include_context=False,include_url=False)]
                            corrected=copy.deepcopy(c.arguments)
                            for f in corrected.get('files',[]):
                                if isinstance(f,dict):f['mode']=420
                            try:
                                WorkspacePatchInput.model_validate_json(json.dumps(corrected,ensure_ascii=False,allow_nan=False));p['mode_only_counterfactual_decode']='PASS'
                            except ValidationError:p['mode_only_counterfactual_decode']='FAIL'
                            patches.append(p)
                        elif isinstance(c,ToolResultContent):
                            call=calls.get(c.call_id);values=c.output if isinstance(c.output,dict) else {}
                            rows.append({'kind':'tool_result','tool':None if call is None else call.tool,'outcome':c.outcome,'error_code':None if c.error is None else c.error.code,'digest_matches_fixture':values.get('content_sha256')==expected})
            result={'authenticated_original_store_read':True,'raw_values_recorded':False,'new_model_requests':0,'effects_executed_by_diagnostic':0,'fixture_unchanged':fixture.endswith(b'return left - right\n'),'approval_items':approvals,'tool_metadata':rows,'patch_calls':patches}
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'patch_calls':patches,'approval_items':approvals,'new_model_requests':0},indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--case',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();sys.path[:0]=[str(args.root/'src'),str(args.root)]
    asyncio.run(diagnose(args.case,args.output))
