import asyncio,json,copy
import pytest
from backend import db,engine,people_search as people,exa_search as exa
from backend.errors import IntegrationError
from test_engine import database,fake_ai,approve_fixture,file,read
from test_exa import mock_api,PAYLOAD

def test_group_search_type_snapshot_and_cache(fake_ai,monkeypatch):
    version=setup(fake_ai,monkeypatch)
    calls=mock_api(monkeypatch,[(200,PAYLOAD)])
    auto=people.create('IT-1',version,['S1'])
    deep=people.create('IT-1',version,['S1'],search_type='deep')
    assert auto['id']!=deep['id']
    assert deep['snapshot']['search_type']=='deep'
    assert people.create('IT-1',version,['S1'],search_type='deep')['id']==deep['id']
    asyncio.run(exa.run(deep['items'][0]['search']['id']))
    assert calls[0][1]['json']['type']=='deep'

def setup(fake_ai,monkeypatch):
    monkeypatch.setenv('EXA_API_KEY','test-key')
    asyncio.run(engine.ingest([file('j','IT-1 JD','job_public')],read,True))
    c=approve_fixture('IT-1');data=json.loads(c['data'])
    data['strategies'][0]['exa_query']='Python engineers professional profiles'
    data['strategies'][1]['exa_query']='Platform engineers professional profiles'
    db.execute('UPDATE configs SET data=? WHERE job_id=?',(db.dumps(data),'IT-1'))
    return c['version']

def test_selected_only_mode_cache_dedup_and_staleness(fake_ai,monkeypatch):
    version=setup(fake_ai,monkeypatch);calls=mock_api(monkeypatch,[(200,PAYLOAD)])
    g=people.create('IT-1',version,['S1'])
    assert len(g['items'])==1
    sid=g['items'][0]['search']['id'];asyncio.run(exa.run(sid))
    g=people.detail(g['id']);assert g['status']=='COMPLETED'
    assert calls[0][1]['json']['category']=='people'
    assert people.create('IT-1',version,['S1'])['id']==g['id']
    both=people.create('IT-1',version,['S2','S1'])
    for item in both['items']:asyncio.run(exa.run(item['search']['id']))
    merged=people.detail(both['id'])
    assert len(calls)==2 and len(merged['results'])==1
    assert set(merged['results'][0]['strategy_ids'])=={'S1','S2'}
    db.execute('UPDATE configs SET version=version+1 WHERE job_id=?',('IT-1',))
    assert not people.detail(g['id'])['is_current']
    with pytest.raises(IntegrationError):people.create('IT-1',version,['S1'])

def test_invalid_selection_and_approval(fake_ai,monkeypatch):
    v=setup(fake_ai,monkeypatch)
    for ids in ([],['S1','S1'],['bad']):
        with pytest.raises(IntegrationError):people.create('IT-1',v,ids)
    db.execute('UPDATE configs SET approved=0')
    with pytest.raises(IntegrationError):people.create('IT-1',v,['S1'])
    assert not db.rows('SELECT * FROM web_searches')

def test_failure_keeps_success(fake_ai,monkeypatch):
    v=setup(fake_ai,monkeypatch);mock_api(monkeypatch,[(200,PAYLOAD),(401,{})])
    g=people.create('IT-1',v,['S1','S2'])
    for item in g['items']:asyncio.run(exa.run(item['search']['id']))
    r=people.detail(g['id']);assert r['status']=='PARTIAL' and len(r['results'])==1

def test_web_people_separate_cache(monkeypatch):
    monkeypatch.setenv('EXA_API_KEY','test-key')
    a=exa.create('Same query');b=exa.create('Same query',mode='people')
    assert a['id']!=b['id'] and a['mode']=='web' and b['mode']=='people'

def test_query_batch_preserves_strategy_and_version(fake_ai,monkeypatch):
    v=setup(fake_ai,monkeypatch);before=json.loads(db.one('SELECT data FROM configs')['data']);calls=[]
    async def fake(task,payload,schema,validate):
        calls.append(task)
        assert all(s['group']=='job_public' for s in payload['public_jd'])
        out={'public_musts':[{'criterion_id':'C1','query':'Python experience'}],'queries':[{'strategy_id':s['id'],'exa_query':'Python professional profiles '+s['id']} for s in before['strategies']]}
        validate(out);return schema.model_validate(out).model_dump()
    monkeypatch.setattr(engine,'ai',fake);asyncio.run(people.draft_queries('IT-1',v))
    row=db.one('SELECT * FROM configs');after=json.loads(row['data'])
    assert calls==['exa_queries'] and row['version']==v+1 and not row['approved']
    for a,b in zip(before['strategies'],after['strategies']):
        assert {k:v for k,v in a.items() if k!='exa_query'}=={k:v for k,v in b.items() if k!='exa_query'}
    assert len(db.rows('SELECT * FROM config_revisions WHERE job_id=?',('IT-1',)))>=2

def test_query_privacy(fake_ai,monkeypatch):
    setup(fake_ai,monkeypatch)
    for q in ('Find staff contact@example.com','Salary USD 5000 professionals'):
        with pytest.raises(IntegrationError):people.validate_query(q,'IT-1')

def test_restart_no_duplicate_selected_tasks(fake_ai,monkeypatch):
    v=setup(fake_ai,monkeypatch);g=people.create('IT-1',v,['S1']);db.init()
    assert people.create('IT-1',v,['S1'])['id']==g['id']
    assert len(db.rows("SELECT * FROM tasks WHERE kind='web_search'"))==1
