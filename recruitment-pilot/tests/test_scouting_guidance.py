import asyncio,copy,json
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from backend import db,engine,people_search,scouting_guidance as guidance
from backend.api import app,approve,VersionBody,update_config
from backend.models import ConfigUpdate
from backend.prompts import PROMPTS
from backend.errors import IntegrationError
from test_engine import database,fake_ai,file,read,approve_fixture,CONFIG

HEADERS={'x-pilot-request':'1'}

def setup(fake_ai):
    asyncio.run(engine.ingest([file('j','IT-1 JD','job_public'),file('j2','IT-2 JD','job_public')],read,True))
    approve_fixture('IT-1')
    return db.one('SELECT * FROM configs WHERE job_id=?',('IT-1',))

def test_append_only_per_job_save_reset_no_calls(fake_ai):
    config=setup(fake_ai);before=copy.deepcopy(config);n=len(fake_ai)
    a=guidance.read('IT-1');assert a['current']['version']==0
    saved=guidance.save('IT-1',0,'Ưu tiên Head; không coi Vice President là tương đương.')
    assert saved['current']['version']==1
    assert guidance.read('IT-2')['current']['version']==0
    assert db.one('SELECT * FROM configs WHERE job_id=?',('IT-1',))==before
    assert len(fake_ai)==n and not db.rows('SELECT * FROM web_searches')
    restored=guidance.save('IT-1',1,guidance.DEFAULT)
    assert restored['current']['version']==2 and len(restored['history'])==2
    assert restored['history'][1]['content']==saved['current']['content']

def test_conflict_and_reload_preserve_existing_version(fake_ai):
    setup(fake_ai);client=TestClient(app)
    url='/api/v1/jobs/IT-1/scouting-guidance'
    assert client.put(url,json={'version':0,'content':'First'},headers=HEADERS).status_code==200
    response=client.put(url,json={'version':0,'content':'Second'},headers=HEADERS)
    assert response.status_code==409
    assert client.get(url).json()['current']['content']=='First'
    assert client.get('/api/v1/jobs/missing/scouting-guidance').status_code==404

@pytest.mark.parametrize('kind',['strategies','exa_queries'])
def test_enqueue_snapshot_old_clients_and_dedup(fake_ai,kind):
    config=setup(fake_ai);guidance.save('IT-1',0,'Prefer Head, not Vice President')
    original=copy.deepcopy(PROMPTS);client=TestClient(app)
    url='/api/jobs/IT-1/strategies' if kind=='strategies' else '/api/v1/jobs/IT-1/exa-queries'
    body={'version':config['version'],'count':2} if kind=='strategies' else {'config_version':config['version']}
    old=client.post(url,json=body,headers=HEADERS);assert old.status_code in (200,202)
    snap=json.loads(db.one('SELECT payload FROM tasks WHERE id=?',(old.json()['task_id'],))['payload'])['guidance']
    assert snap['version']==0 and snap['content']==guidance.DEFAULT
    body['guidance_version']=1
    first=client.post(url,json=body,headers=HEADERS)
    assert client.post(url,json=body,headers=HEADERS).json()==first.json()
    payload=json.loads(db.one('SELECT payload FROM tasks WHERE id=?',(first.json()['task_id'],))['payload'])
    guidance.save('IT-1',1,'Changed during work')
    assert payload['guidance']['content']=='Prefer Head, not Vice President'
    assert payload['guidance']['prompt_hash']==engine.digest(payload['guidance']['prompt'])
    assert PROMPTS==original

@pytest.mark.parametrize('kind',['strategies','exa_queries'])
def test_generation_uses_snapshot_preserves_criteria_and_query_strategy(fake_ai,monkeypatch,kind):
    monkeypatch.setenv('EXA_API_KEY','isolated-test-key')
    config=setup(fake_ai);before=json.loads(config['data'])
    guidance.save('IT-1',0,'Prefer Head, not Vice President')
    snap=guidance.capture('IT-1',1,kind);guidance.save('IT-1',1,'Later edit')
    calls=[]
    async def fake(task,payload,schema,validate,**kwargs):
        calls.append(task);assert kwargs['prompt_override']==snap['prompt']
        assert payload['recruiter_guidance']==snap['content']
        assert 'authorized professional scouting input' in kwargs['prompt_override']
        assert 'Each exa_query must itself express' in kwargs['prompt_override']
        assert 'Prefer Head, not Vice President' in kwargs['prompt_override']
        assert 'Later edit' not in kwargs['prompt_override']
        value={'public_musts':[{'criterion_id':'C1','query':'Python experience'}]}
        if kind=='strategies':value['strategies']=copy.deepcopy(CONFIG['strategies'])
        else:value['queries']=[{'strategy_id':s['id'],'exa_query':'Python professional profiles '+s['id']} for s in before['strategies']]
        validate(value);return schema.model_validate(value).model_dump()
    monkeypatch.setattr(engine,'ai',fake)
    if kind=='strategies':asyncio.run(engine.generate_strategies('IT-1',config['version'],2,snap))
    else:asyncio.run(people_search.draft_queries('IT-1',config['version'],snap))
    after=db.one('SELECT * FROM configs WHERE job_id=?',('IT-1',));data=json.loads(after['data'])
    assert calls==[kind] and after['version']==config['version']+1 and not after['approved']
    assert data['criteria']==before['criteria'] and data['scouting_guidance']['version']==1
    if kind=='exa_queries':
        for a,b in zip(before['strategies'],data['strategies']):
            assert {k:v for k,v in a.items() if k!='exa_query'}=={k:v for k,v in b.items() if k!='exa_query'}
    approve('IT-1',VersionBody(version=after['version']))
    group=people_search.create('IT-1',after['version'],['S1'],location_scope='VIETNAM')
    assert group['snapshot']['scouting_guidance']==data['scouting_guidance']
    # Client config edits cannot forge the generation's audit metadata.
    editable=copy.deepcopy(data);editable['scouting_guidance']={'version':999}
    update_config('IT-1',ConfigUpdate(version=after['version'],config=editable))
    assert json.loads(db.one('SELECT data FROM configs WHERE job_id=?',('IT-1',))['data'])['scouting_guidance']==data['scouting_guidance']

@pytest.mark.parametrize('failure',['ai','stale','must','privacy','length'])
def test_failed_generation_keeps_current_config(fake_ai,monkeypatch,failure):
    config=setup(fake_ai);guidance.save('IT-1',0,'Business instructions')
    snap=guidance.capture('IT-1',1,'strategies');before=config['data']
    async def fake(task,payload,schema,validate,**kwargs):
        if failure=='ai':raise IntegrationError('AI unavailable')
        out={'strategies':copy.deepcopy(CONFIG['strategies']),'public_musts':[{'criterion_id':'C1','query':'Python experience'}]}
        if failure=='must':out['public_musts']=[]
        if failure=='privacy':out['strategies'][0]['exa_query']='Find people with salary USD 5000'
        if failure=='length':out['strategies'][0]['exa_query']='Python '*250
        if failure=='stale':db.execute('UPDATE configs SET version=version+1 WHERE job_id=?',('IT-1',))
        validate(out);return out
    monkeypatch.setattr(engine,'ai',fake)
    with pytest.raises(IntegrationError):asyncio.run(engine.generate_strategies('IT-1',config['version'],2,snap))
    assert db.one('SELECT data FROM configs WHERE job_id=?',('IT-1',))['data']==before
    assert guidance.read('IT-1')['current']['version']==1

def test_guidance_version_cannot_cross_jobs(fake_ai):
    setup(fake_ai);guidance.save('IT-1',0,'Only this JD')
    with pytest.raises(HTTPException):guidance.capture('IT-2',1,'strategies')
    db.init()
    assert guidance.read('IT-1')['current']['content']=='Only this JD'

def test_runtime_prompt_override_is_per_call(monkeypatch):
    from backend import ai_runtime
    from backend.models import ExaQuery
    from types import SimpleNamespace
    original=copy.deepcopy(PROMPTS);observed=[]
    async def generate(*args):
        observed.append(args[3])
        return json.dumps({'strategy_id':'S1','exa_query':'Python profiles'}),{}
    monkeypatch.setattr(ai_runtime,'key',lambda provider:'isolated-test-key')
    monkeypatch.setattr(ai_runtime,'adapter',lambda provider:SimpleNamespace(generate=generate))
    async def run():
        await ai_runtime.ai('exa_queries',{},ExaQuery,prompt_override='JOB ONE OVERRIDE')
        await ai_runtime.ai('exa_queries',{},ExaQuery)
    asyncio.run(run())
    assert observed[0].startswith('JOB ONE OVERRIDE\nJSON schema:')
    assert observed[1].startswith(original['exa_queries'])
    assert PROMPTS==original
