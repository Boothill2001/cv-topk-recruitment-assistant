import asyncio, json
import pytest
from backend import db, engine, people_search, assessment_groups as groups, public_assessment
from test_engine import database, fake_ai
from test_public_assessment import setup
from test_exa import mock_api

def prepare(fake_ai, monkeypatch, count, missing=False, long=False):
    version = setup(fake_ai)
    sources = [{'url': f'https://example.com/p{i:03}', 'title': f'Person {i}', 'highlights': ['Python']} for i in range(count)]
    search = {'id':'G','job_id':'IT-1','status':'COMPLETED','is_current':True,'snapshot':{},'results':sources}
    monkeypatch.setattr(people_search, 'detail', lambda _: search)
    monkeypatch.setenv('EXA_API_KEY','test-key')
    responses = []
    for offset in range(0,count,20):
        response = {'results': [{'url':s['url'],'text':(''.join(str(i)+'x'*1000 for i in range(1100)) if long and s==sources[0] else 'Worked with Python')} for s in sources[offset:offset+20] if not (missing and s==sources[0])], 'costDollars':{'total':.001}}
        response['statuses']=[{'id':s['url'],'status':'success','source':'cached'} for s in response['results']]
        responses.append((200,response))
    calls = mock_api(monkeypatch,responses); judges = []
    async def judge(task,payload,schema,validate,**kw):
        judges.append([s['url'] for s in payload['sources']]); kw['on_attempt'](); kw['on_usage']({'total_tokens':123},'COMPLETED')
        answer = {'items':[{'id':s['id'],'a':[{'c':c['id'],'s':'UNKNOWN','why':'Chưa đủ dữ liệu','q':[],'questions':[]} for c in payload['criteria']]} for s in payload['sources']]}
        result = schema.model_validate(answer).model_dump(); validate(result); return result
    monkeypatch.setattr(engine,'ai',judge)
    return version, sources, calls, judges

@pytest.mark.parametrize('count',[10,20,38])
def test_all_sources_stable_cache_idempotence_restart(fake_ai,monkeypatch,count):
    version,sources,calls,judges=prepare(fake_ai,monkeypatch,count)
    g=groups.create('G',version)
    assert groups.create('G',version)['id']==g['id']
    assert not calls and not judges  # confirmation enqueues only; preview/history cost nothing
    asyncio.run(groups.run(g['id'])); report=groups.detail(g['id'])
    assert report['status']=='COMPLETED' and report['assessed_sources']==count, (report['error'], report['batches'], report['content_fetches'])
    assert len(calls)==(count+19)//20
    assert [u for batch in judges for u in batch]==[s['url'] for s in sources]
    assert all(len(batch)<=5 for batch in judges)
    assert len({r['candidate_id'] for r in report['results']})==count
    assert all(r['score']==0 and r['assessments'][0]['status']=='UNKNOWN' for r in report['results'])
    assert not db.rows("SELECT id FROM profiles WHERE kind='candidate'")
    assert not db.rows("SELECT id FROM tasks WHERE kind IN ('content_fetch','public_assessment')")
    db.init(); asyncio.run(groups.run(g['id']))
    assert len(groups.detail(g['id'],7)['results'])==7 and len(judges)==(count+4)//5
    assert groups.create('G',version)['id']==g['id']

def test_missing_and_oversize_do_not_lose_other_sources(fake_ai,monkeypatch):
    version,sources,_,judges=prepare(fake_ai,monkeypatch,10,long=True)
    g=groups.create('G',version); asyncio.run(groups.run(g['id'])); r=groups.detail(g['id'])
    assert r['status']=='PARTIAL' and r['assessed_sources']==9
    assert 'Nguồn đơn lẻ' in r['unassessed_sources'][0]['reason']
    assert sum(map(len,judges))==9

def test_missing_never_uses_highlights(fake_ai,monkeypatch):
    version,sources,_,judges=prepare(fake_ai,monkeypatch,10,missing=True)
    g=groups.create('G',version); asyncio.run(groups.run(g['id'])); r=groups.detail(g['id'])
    assert r['status']=='PARTIAL' and r['assessed_sources']==9
    assert sources[0]['url'] not in [u for batch in judges for u in batch]
    assert len(r['unassessed_sources'])==1

def test_budget_packing_preserves_every_url(fake_ai,monkeypatch):
    version,_,_,_=prepare(fake_ai,monkeypatch,38)
    from backend import ai_assessment
    original=ai_assessment.model_budget()
    monkeypatch.setattr(ai_assessment,'model_budget',lambda:{**original,'max_cells':6})
    g=groups.create('G',version); asyncio.run(groups.run(g['id'])); r=groups.detail(g['id'])
    assert r['assessed_sources']==38
    assert [len(b['urls']) for b in r['batches']]==[3]*12+[2]

def test_retry_keeps_successes_and_restart_resumes(fake_ai,monkeypatch):
    version,_,calls,judges=prepare(fake_ai,monkeypatch,38)
    original=public_assessment.assess
    failed=[]
    async def fail_once(aid):
        if len(judges)==1 and not failed:
            failed.append(aid)
            db.execute("UPDATE public_assessments SET status='FAILED',attempts=1,error='synthetic timeout' WHERE id=?",(aid,))
        else: await original(aid)
    monkeypatch.setattr(public_assessment,'assess',fail_once)
    g=groups.create('G',version); asyncio.run(groups.run(g['id']))
    assert groups.detail(g['id'])['assessed_sources']==33
    groups.retry(g['id']); db.execute("UPDATE assessment_groups SET status='RUNNING' WHERE id=?",(g['id'],))
    db.execute("UPDATE tasks SET status='RUNNING' WHERE kind='assessment_group'")
    db.init(); asyncio.run(groups.run(g['id']))
    assert groups.detail(g['id'])['assessed_sources']==38
    assert len(calls)==2 and len(judges)==8

def test_routes_and_input_drift(fake_ai,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app
    version,_,calls,judges=prepare(fake_ai,monkeypatch,10)
    client=TestClient(app,headers={'X-Pilot-Request':'1'}); response=client.post('/api/v1/people-searches/G/assessment-groups',json={'config_version':version})
    assert response.status_code==202
    gid=response.json()['id']
    assert client.get('/api/v1/assessment-groups/'+gid+'/results?limit=7').status_code==200
    db.set_setting('model','changed-model'); asyncio.run(groups.run(gid))
    assert not calls and not judges
    assert not groups.detail(gid)['is_current'] and groups.detail(gid)['status']=='PARTIAL'

def test_preview_only_cache_and_expiry(fake_ai,monkeypatch):
    from backend import content_fetch
    from datetime import datetime,timedelta,timezone
    version,sources,calls,judges=prepare(fake_ai,monkeypatch,10)
    before=db.rows('SELECT * FROM tasks')
    assert groups.preview('G',version)['cached_available']==0
    assert db.rows('SELECT * FROM tasks')==before and not calls and not judges
    fetch=content_fetch.create('G',[s['url'] for s in sources],enqueue=False)
    asyncio.run(content_fetch.run(fetch['id']))
    p=groups.preview('G',version)
    assert p['cached_available']==10 and len(p['cached_batches'])==2
    assert len(calls)==1 and not judges and db.rows('SELECT * FROM tasks')==before
    data=json.loads(db.one('SELECT data FROM content_fetches WHERE id=?',(fetch['id'],))['data'])
    stale=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    for item in data['results']:item['fetched_at']=stale
    db.execute('UPDATE content_fetches SET data=? WHERE id=?',(db.dumps(data),fetch['id']))
    assert groups.preview('G',version)['cached_available']==0
    fresh=content_fetch.create('G',[s['url'] for s in sources],enqueue=False)
    assert fresh['id']!=fetch['id'] and all(s['status']=='PENDING' for s in fresh['results'])

def test_duplicate_urls_and_parent_evidence_view(fake_ai,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app
    version,sources,_,_=prepare(fake_ai,monkeypatch,10)
    sources.append(dict(sources[0]))
    g=groups.create('G',version);asyncio.run(groups.run(g['id']))
    assert groups.detail(g['id'])['total_sources']==10
    r=groups.detail(g['id'])['results'][0]
    client=TestClient(app)
    response=client.get('/api/v1/assessment-groups/'+g['id']+'/sources/'+r['candidate_id'])
    assert response.status_code==200 and response.json()['text']=='Worked with Python'
    assert response.json()['url']==r['url']

def test_linkedin_aliases_are_one_source_without_merging_names():
    from backend.source_identity import distinct_sources, profile_key
    first={'url':'https://linkedin.com/in/nguyenqnh','title':'Quách Nguyên','strategy_ids':['S1']}
    alias={'url':'https://vn.linkedin.com/in/nguyenqnh/?trk=search','title':'Quách Nguyên','strategy_ids':['S2']}
    different={'url':'https://linkedin.com/in/different-person','title':'Quách Nguyên'}
    result=distinct_sources([first,alias,different])
    assert len(result)==2
    assert result[0]['url']==first['url']
    assert result[0]['source_urls']==[first['url'],alias['url']]
    assert result[0]['strategy_ids']==['S1','S2']
    assert profile_key('https://linkedin.com.evil.com/in/nguyenqnh')!=profile_key(first['url'])
    assert 'source_urls' not in first  # inputs/history never mutated

def test_new_engine_does_not_rewrite_old_group(fake_ai,monkeypatch):
    version,_,_,_=prepare(fake_ai,monkeypatch,10)
    g=groups.create('G',version)
    snapshot=groups.row(g['id'])['snapshot'];snapshot['version']='all-public-sources-1'
    db.execute('UPDATE assessment_groups SET snapshot=? WHERE id=?',(db.dumps(snapshot),g['id']))
    assert not groups.detail(g['id'])['is_current']
    assert groups.row(g['id'])['snapshot']['version']=='all-public-sources-1'
