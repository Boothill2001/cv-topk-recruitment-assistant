import asyncio,json
import httpx,pytest
from backend import db,jev,public_assessment as service,content_fetch
from backend.errors import IntegrationError
from test_engine import database,fake_ai
from test_public_assessment import setup

def content(version):
    now=db.now();text='Worked with Python for 3 years.\n\nCompany overview: SQL services.'
    item={'url':'https://example.com/person','status':'AVAILABLE','text':text,'paragraphs':content_fetch.blocks(text),
          'revision':'revision1','provider_source':'cached','fetched_at':now}
    db.execute('INSERT INTO content_fetches VALUES(?,?,?,?,?,?,?,?,?,?)',('F','fetch','S',db.dumps({}),
        'COMPLETED',db.dumps({'results':[item],'usage':[]}),0,None,now,now))
    return service.create('IT-1',version,'S',content_fetch_id='F',assessment_engine='JEV')

def stub(monkeypatch,choices=None,status=200):
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,headers,json):
            calls.append(json)
            assert url=='https://api.typesafe.ai/v1/systemone'
            assert headers['Authorization']=='Bearer secret-test-key'
            answers={}
            for qid,q in json['questions'].items():
                options=list(q['criteria']);selected=(choices(qid,q) if choices else ('SUPPORT' if 'SUPPORT' in options else 'MET'))
                answers[qid]={'type':'choice','choice':selected,'confidence':.9,
                    'probabilities':{o:float(o==selected) for o in options}}
            return httpx.Response(status,json={'model':jev.MODEL,'answers':answers,'usage':{'input_tokens':100,'output_tokens':0}})
    monkeypatch.setenv('TYPESAFE_API_KEY','secret-test-key');monkeypatch.setattr(jev.httpx,'AsyncClient',Client)
    return calls

def test_exact_evidence_current_compatibility_and_cache(fake_ai,monkeypatch):
    version=setup(fake_ai);calls=stub(monkeypatch);row=content(version)
    current=service.create('IT-1',version,'S',content_fetch_id='F')
    assert current['assessment_engine']=='CURRENT' and current['id']!=row['id']
    assert not calls
    assert service.create('IT-1',version,'S',content_fetch_id='F',assessment_engine='JEV',preview=True)['fits']
    assert not calls
    asyncio.run(service.assess(row['id']));result=service.detail(row['id'])
    assert result['status']=='COMPLETED',result
    assert len(calls)==2 and result['results'][0]['score']==100
    for a in result['results'][0]['assessments']:
        for e in a['evidence']:
            assert e['revision']=='revision1' and e['quote'] in result['sources'][0]['evidence'][e['paragraph_id']]
    asyncio.run(service.assess(row['id']));db.init()
    assert len(calls)==2 and service.detail(row['id'])['status']=='COMPLETED'
    assert 'secret-test-key' not in db.dumps(db.rows('SELECT * FROM jev_steps'))
    assert result['usage']['estimated_cost_dollars']==.0000084

def test_no_evidence_unknown_and_no_second_call(fake_ai,monkeypatch):
    version=setup(fake_ai);calls=stub(monkeypatch,lambda *_:'NONE');row=content(version)
    asyncio.run(service.assess(row['id']));r=service.detail(row['id'])
    assert len(calls)==1 and all(a['status']=='UNKNOWN' and not a['evidence'] for a in r['results'][0]['assessments'])

def test_401_fatal_no_retry(fake_ai,monkeypatch):
    version=setup(fake_ai);calls=stub(monkeypatch,status=401);row=content(version)
    asyncio.run(service.assess(row['id']));r=service.detail(row['id'])
    assert r['status']=='PARTIAL' and not r['results'] and len(calls)==1
    assert r['steps'][0]['status']=='FATAL' and jev.trial_budget()['committed_estimate_dollars']==0
    asyncio.run(service.assess(row['id']));assert len(calls)==1

def test_limits_and_budget_no_provider(fake_ai,monkeypatch):
    row=content(setup(fake_ai));snap=row['input_snapshot'];source=snap['sources'][0]
    source['evidence']={'PUB001.P001':'a'*40000}
    assert not jev.preview(snap)['fits']
    source['evidence']={'PUB001.P001':'Python'}
    calls=stub(monkeypatch);monkeypatch.setattr(jev,'TRIAL_NANO',1)
    asyncio.run(service.assess(row['id']))
    assert not calls

def test_pack_lossless_and_years_all_any():
    source={'id':'PUB001','revision':'r','evidence':{'PUB001.P1':'Python '*100},'context':['Company SQL']}
    criteria=[{'id':'C1','name':'3+ years Python AND SQL','rule':{'mode':'ALL','years':3}}]
    requests,mapping=jev.evidence_requests(source,criteria)
    assert mapping['Q1']==('C1','PUB001.P1')
    state=requests[0]['state']
    assert {r:state['text'][v['start']:v['end']] for r,v in state['professional_paragraphs'].items()}==source['evidence']
    assert 'full_source_text' not in state
    assert requests[0]['questions']['Q1']['instructions']['criterion']==criteria[0]
    assert 'Company SQL' not in mapping.values()

def test_response_rejects_wrong_model_missing_ids_and_probabilities():
    payload={'questions':{'Q1':{'criteria':{'A':'a','B':'b'}}}}
    good={'model':jev.MODEL,'answers':{'Q1':{'type':'choice','choice':'A','confidence':1,'probabilities':{'A':1,'B':0}}},'usage':{'input_tokens':1}}
    jev.validate_answers(good,payload)
    for mutate in (lambda v:v.update(model='other'),lambda v:v.update(answers={}),lambda v:v['answers']['Q1'].update(confidence=float('nan'))):
        value=json.loads(json.dumps(good));mutate(value)
        with pytest.raises(IntegrationError):jev.validate_answers(value,payload)

def test_temporary_errors_three_total_and_resume_completed_steps(fake_ai,monkeypatch):
    version=setup(fake_ai);calls=stub(monkeypatch,status=503);row=content(version)
    async def no_sleep(*args):pass
    monkeypatch.setattr(jev.asyncio,'sleep',no_sleep)
    asyncio.run(service.assess(row['id']))
    assert len(calls)==3 and service.detail(row['id'])['steps'][0]['attempts']==3
    assert jev.trial_budget()['committed_estimate_dollars']>0
    # Unknown charges remain reserved rather than being reported as free.
    assert service.detail(row['id'])['usage']['has_unreported_cost']
    service.retry(row['id']);asyncio.run(service.assess(row['id']))
    assert len(calls)==3

def test_restart_resumes_only_unfinished_phase(fake_ai,monkeypatch):
    row=content(setup(fake_ai));calls=stub(monkeypatch);snap=row['input_snapshot'];source=snap['sources'][0]
    first,_=jev.evidence_requests(source,snap['criteria'])
    asyncio.run(jev.execute(row['id'],source,'evidence:0',first[0]))
    db.execute("UPDATE public_assessments SET status='RUNNING' WHERE id=?",(row['id'],))
    db.execute("UPDATE tasks SET status='INTERRUPTED' WHERE kind='public_assessment'")
    db.init();assert service.detail(row['id'])['status']=='PENDING'
    asyncio.run(service.assess(row['id']))
    assert len(calls)==2 and service.detail(row['id'])['status']=='COMPLETED'

def test_source_and_revision_are_bound_in_step_fingerprint(fake_ai,monkeypatch):
    row=content(setup(fake_ai));calls=stub(monkeypatch);source=row['sources'][0]
    payload=jev.evidence_requests(source,row['input_snapshot']['criteria'])[0][0]
    asyncio.run(jev.execute(row['id'],source,'evidence:0',payload))
    asyncio.run(jev.execute(row['id'],source,'evidence:0',payload))
    with pytest.raises(IntegrationError):asyncio.run(jev.execute(row['id'],{**source,'revision':'revision2'},'evidence:0',payload))
    payload={**payload,'state':{**payload['state'],'revision':'revision2'}}
    asyncio.run(jev.execute(row['id'],{**source,'revision':'revision2'},'evidence:0',payload))
    assert len(calls)==2

def test_parent_group_cached_only_per_source_isolation_and_topk(fake_ai,monkeypatch):
    from test_assessment_groups import prepare
    from backend import assessment_groups as groups
    version,sources,exa_calls,current_calls=prepare(fake_ai,monkeypatch,3)
    now=db.now()
    items=[{'url':s['url'],'title':s['title'],'status':'AVAILABLE','text':'Worked with Python',
        'revision':'revision-'+str(i),'provider_source':'cached','fetched_at':now,
        'adapter_version':content_fetch.VERSION,'paragraphs':content_fetch.blocks('Worked with Python')} for i,s in enumerate(sources)]
    db.execute('INSERT INTO content_fetches VALUES(?,?,?,?,?,?,?,?,?,?)',('CACHE','cache','G',db.dumps({}),
        'COMPLETED',db.dumps({'results':items,'usage':[],'known_cost_dollars':0,'has_unreported_cost':False}),0,None,now,now))
    monkeypatch.setattr(content_fetch,'create',lambda *args,**kwargs:content_fetch.detail('CACHE'))
    calls=stub(monkeypatch)
    preview=groups.preview('G',version,'JEV')
    assert preview['cached_available']==3 and len(preview['cached_batches'])==3 and not calls
    parent=groups.create('G',version,'JEV')
    assert parent['assessment_engine']=='JEV'
    db.set_setting('model','different-current-engine-model')
    asyncio.run(groups.run(parent['id']));r=groups.detail(parent['id'])
    assert r['status']=='COMPLETED' and r['assessed_sources']==3,(r['error'],r['unassessed_sources'])
    assert len(calls)==6 and not exa_calls and not current_calls
    assert all(len(p['state']['professional_paragraphs'])==1 for p in calls)
    assert len(groups.detail(parent['id'],1)['results'])==1 and len(calls)==6
    assert groups.create('G',version,'JEV')['id']==parent['id']

def test_api_engine_validation_and_no_highlights(fake_ai):
    from fastapi.testclient import TestClient
    from backend.api import app
    version=setup(fake_ai);client=TestClient(app,headers={'X-Pilot-Request':'1'})
    body={'job_id':'IT-1','config_version':version,'search_id':'S','assessment_engine':'OTHER'}
    assert client.post('/api/v1/public-assessment-preview',json=body).status_code==422
    body['assessment_engine']='JEV'
    assert client.post('/api/v1/public-assessments',json=body).status_code==409
    assert not db.rows('SELECT id FROM jev_steps')

def test_explicit_credential_retry_and_contrary_evidence(fake_ai,monkeypatch):
    row=content(setup(fake_ai));first=stub(monkeypatch,status=401)
    asyncio.run(service.assess(row['id']));assert len(first)==1
    calls=stub(monkeypatch,lambda qid,q:'CONTRARY' if 'CONTRARY' in q['criteria'] else 'NOT_MET')
    service.retry(row['id']);asyncio.run(service.assess(row['id']))
    result=service.detail(row['id'])
    assert result['status']=='COMPLETED' and len(calls)==2
    assert all(a['status']=='NOT_MET' and a['evidence'] for a in result['results'][0]['assessments'])
    assert result['steps'][0]['attempts']==2

def test_partial_source_preserves_validated_results(fake_ai,monkeypatch):
    row=content(setup(fake_ai));snap=row['input_snapshot']
    snap['sources'].append({**snap['sources'][0],'id':'PUB002','url':'https://example.com/other','revision':'other',
        'evidence':{'PUB002.P001':'SQL'},'paragraph_kinds':{'PUB002.P001':'professional'}})
    db.execute('UPDATE public_assessments SET snapshot=? WHERE id=?',(db.dumps(snap),row['id']))
    calls=stub(monkeypatch);original=jev.execute
    async def fail_second(aid,source,phase,payload):
        if source['id']=='PUB002':raise IntegrationError('Synthetic temporary failure')
        return await original(aid,source,phase,payload)
    monkeypatch.setattr(jev,'execute',fail_second)
    asyncio.run(service.assess(row['id']));r=service.detail(row['id'])
    assert r['status']=='PARTIAL' and len(r['results'])==1
    assert r['results'][0]['url']=='https://example.com/person'
    assert 'https://example.com/other' in r['source_errors']

def test_postgres_numeric_sum_in_budget(monkeypatch):
    from decimal import Decimal
    original=db.one
    monkeypatch.setattr(db,'one',lambda sql,*a: {'n':Decimal('4200')} if 'SUM(charged_nano)' in sql else original(sql,*a))
    assert jev.trial_budget()['committed_estimate_dollars']==.0000042

def test_known_bad_key_blocks_confirmation_until_file_changes(fake_ai,monkeypatch,tmp_path):
    import os
    row=content(setup(fake_ai))
    monkeypatch.delenv('TYPESAFE_API_KEY',raising=False)
    path=tmp_path/'external-key.env';path.write_text('TYPESAFE_API_KEY=test-only-secret')
    db.set_setting('env_file:typesafe',str(path));jev.record_auth_failure(401)
    assert jev.credential_problem()
    assert not jev.preview(row['input_snapshot'])['fits']
    with pytest.raises(IntegrationError):
        service.create('IT-1',row['config_version'],'S',content_fetch_id='F',assessment_engine='JEV')
    assert not db.rows('SELECT id FROM jev_steps')
    assert 'test-only-secret' not in db.dumps(db.rows('SELECT * FROM settings'))
    previous=path.stat().st_mtime_ns;path.write_text('TYPESAFE_API_KEY=updated-test-only-secret')
    os.utime(path,ns=(previous+1000000000,previous+1000000000))
    assert jev.credential_problem() is None
