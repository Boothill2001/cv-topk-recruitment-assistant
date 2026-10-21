import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from backend import db, engine, inbound_review as service, search_orchestration, jev
from backend.api import app
from test_engine import database, fake_ai
from test_internal_jev import setup_pool
from test_jev import stub


def raw_cv(cid, text='Worked with Python for 3 years.'):
    db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               ('raw:'+cid,cid+' CV','text/plain','https://example.org/'+cid,'candidate_public',cid,'1',engine.digest(text),text,'READY',None,1,1,db.now()))


def freeze(ids, version):
    s=service.snapshot('IT-1',version,ids)
    return service.freeze('IT-1',version,ids,service.fingerprint(s))


def test_explicit_twenty_cvs_bypass_retrieval_and_structured_profile(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);calls=stub(monkeypatch)
    monkeypatch.setattr(engine,'create_run',lambda *a,**k:pytest.fail('retrieval must not choose this set'))
    for i in range(2,22):raw_cv('CV'+str(i))
    ids=['CV21']+['CV'+str(i) for i in range(2,21)]
    snap=service.snapshot('IT-1',version,ids)
    p=service.preflight(snap)
    assert p['selected_count']==20 and p['text_count']==20 and list(snap['group'])==ids
    assert not calls and not db.rows('SELECT * FROM searches') and not db.rows('SELECT * FROM runs')
    r=freeze(ids,version)
    frozen=json.loads(db.one('SELECT snapshot FROM inbound_review_sets WHERE id=?',(r['id'],))['snapshot'])
    assert isinstance(frozen['jev']['sources'][0]['raw_text'],dict)
    assert len(db.rows("SELECT id FROM artifacts WHERE kind='source-text'"))==1  # Same CV body stored once, never merge candidate identities.
    assert service.freeze('IT-1',version,ids,p['fingerprint'])['id']==r['id']
    assert not db.rows('SELECT * FROM tasks WHERE kind=?',('batch_assessment',))
    assert 'PRIVATE SECRET' not in db.dumps(snap)


def test_preview_change_duplicates_missing_and_offsets(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);stub(monkeypatch)
    text='Worked with Python for 3 years.\n\nWorked with Python for 3 years.'
    raw_cv('CV2',text)
    snap=service.snapshot('IT-1',version,['CV2'])
    source=snap['jev']['sources'][0]
    spans=list(source['evidence_offsets'].values())
    assert len(spans)==2 and spans[0]['start']!=spans[1]['start']
    from backend.jev_planner import parts
    manifest=parts(source)
    assert ''.join(p['text'] for p in manifest)==text
    assert len(manifest[0]['professional_paragraphs'])==2
    raw_cv('CV4','Worked with Python.\n\nCompany overview: SQL services.')
    company=service.snapshot('IT-1',version,['CV4'])['jev']['sources'][0]
    assert all('Company overview' not in text for text in company['evidence'].values())
    assert company['context']==['Company overview: SQL services.']
    long_company='Company overview: '+('Python SQL services. '*300)
    raw_cv('CV5','Worked with Python.\n\n'+long_company)
    source=service.snapshot('IT-1',version,['CV5'])['jev']['sources'][0]
    assert list(source['evidence'].values())==['Worked with Python.']
    assert ''.join(source['context'])==long_company
    bad=json.loads(db.dumps(source));next(iter(bad['evidence_offsets'].values()))['start']+=1
    with pytest.raises(Exception):parts(bad)
    with pytest.raises(Exception):service.snapshot('IT-1',version,['CV2','CV2'])
    with pytest.raises(Exception):service.snapshot('IT-1',version,['CV99999'])
    fp=service.fingerprint(snap)
    db.execute("UPDATE files SET revision=2,text='Changed source',hash=? WHERE id='raw:CV2'",(engine.digest('Changed source'),))
    with pytest.raises(Exception):service.freeze('IT-1',version,['CV2'],fp)
    raw_cv('CV3','')
    missing=service.snapshot('IT-1',version,['CV3'])
    assert 'CV3' in missing['jev']['missing'] and len(missing['group'])==1


def test_real_pipeline_mock_provider_blind_review_and_idempotence(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);calls=stub(monkeypatch)
    raw_cv('CV2')
    r=freeze(['CV2','CV1'],version);rid=r['id']
    sid=service.start(rid,True)['search_id']
    assert service.start(rid,True)['search_id']==sid
    assert len(db.rows("SELECT * FROM tasks WHERE kind='batch_assessment'"))==1
    asyncio.run(search_orchestration.assess(sid))
    assert service.detail(rid)['scored_count']==2
    client=TestClient(app,headers={'X-Pilot-Request':'1'})
    assert client.get('/api/v1/searches/'+sid).status_code==409
    assert client.get('/api/v1/pipeline-runs/by-origin/assessment/'+sid).status_code==409
    assert client.get('/api/v1/inbound-review-sets/'+rid+'/results').status_code==409
    assert client.get('/api/v1/inbound-review-sets/'+rid+'/export').status_code==409
    assert client.post('/api/v1/inbound-review-sets/'+rid+'/review/reveal').status_code==409
    blind=client.get('/api/v1/inbound-review-sets/'+rid+'/review').json()
    assert 'score' not in db.dumps(blind) and 'be_rank' not in db.dumps(blind)
    for cid in ['CV1','CV2']:
        body={'candidate_id':cid,'version':0,'decision':'CONTACT','reason':'Human review before AI score','criterion_ids':['C1']}
        assert client.post('/api/v1/inbound-review-sets/'+rid+'/review',json=body).status_code==200
        assert client.post('/api/v1/inbound-review-sets/'+rid+'/review',json=body).status_code==409
    assert client.post('/api/v1/inbound-review-sets/'+rid+'/review/reveal').status_code==200
    run_id=db.one('SELECT result_run_id FROM searches WHERE id=?',(sid,))['result_run_id']
    assert client.get('/api/runs/'+run_id).status_code==200
    assert client.post('/api/v1/searches/'+sid+'/evidence-reviews',json={'candidate_id':'CV1','criterion_id':'C1','decision':'INSUFFICIENT','note':'Fixture review only'}).status_code==200
    result=client.get('/api/v1/inbound-review-sets/'+rid+'/results?limit=1').json()
    assert len(result['results'])==1 and result['total_scored']==2
    assert result['metrics']['quality_accepted'] is False
    assert client.get('/api/v1/inbound-review-sets/'+rid+'/export').status_code==200
    n=len(calls);assert service.start(rid,True,True)['search_id']==sid
    assert len(calls)==n


def test_partial_results_survive_budget_pause_and_restart(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);calls=stub(monkeypatch);raw_cv('CV2')
    rid=freeze(['CV1','CV2'],version)['id'];sid=service.start(rid,True)['search_id']
    execute=jev.execute
    async def limited(aid,source,phase,payload):
        if source['url']=='CV2':raise jev.BudgetWaiting('Chờ ngân sách thử nghiệm')
        return await execute(aid,source,phase,payload)
    monkeypatch.setattr(jev,'execute',limited)
    asyncio.run(search_orchestration.assess(sid))
    d=service.detail(rid)
    assert d['scored_count']==1 and d['candidates'][1]['status']=='WAITING_BUDGET'
    first=len(calls)
    monkeypatch.setattr(jev,'execute',execute)
    db.execute("UPDATE searches SET status='RUNNING' WHERE id=?",(sid,));db.init()
    assert db.one('SELECT status FROM searches WHERE id=?',(sid,))['status']=='INTERRUPTED'
    service.start(rid,True,True);asyncio.run(search_orchestration.assess(sid))
    assert service.detail(rid)['scored_count']==2 and len(calls)==first+2
    assert len(db.rows('SELECT * FROM search_assessments WHERE search_id=?',(sid,)))==2


def test_online_disabled_and_preview_no_provider(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);stub(monkeypatch)
    profile=json.loads(db.one("SELECT data FROM profiles WHERE id='CV1'")['data'])
    profile['title']='PRIVATE NOTE DERIVED TITLE'
    db.execute("UPDATE profiles SET data=? WHERE id='CV1'",(db.dumps(profile),))
    assert 'PRIVATE NOTE' not in db.dumps(service.snapshot('IT-1',version,['CV1']))
    client=TestClient(app,headers={'X-Pilot-Request':'1'})
    preview=client.post('/api/v1/jobs/IT-1/inbound-review-sets/preview',json={'candidate_ids':['CV1'],'config_version':version})
    assert preview.status_code==200
    assert not db.rows('SELECT * FROM jev_steps') and not db.rows('SELECT * FROM jev_ledger')
    monkeypatch.setenv('PILOT_MODE','ONLINE_PILOT')
    with pytest.raises(Exception):service.catalog()


def test_uncertain_request_snapshot_tampering_and_ledger_preserved(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);calls=stub(monkeypatch)
    rid=freeze(['CV1'],version)['id'];sid=service.start(rid,True)['search_id']
    snap=service.stored_snapshot(db.one('SELECT snapshot FROM searches WHERE id=?',(sid,)))
    requests,_=jev.evidence_requests(snap['jev']['sources'][0],snap['jev']['criteria'])
    asyncio.run(jev.execute(sid,snap['jev']['sources'][0],'evidence:0',requests[0]))
    ledger=db.rows('SELECT * FROM jev_ledger');n=len(calls)
    db.execute("UPDATE jev_steps SET status='UNCERTAIN',response=NULL WHERE assessment_id=?",(sid,))
    db.execute("UPDATE searches SET status='INTERRUPTED' WHERE id=?",(sid,))
    with pytest.raises(Exception):service.start(rid,True,True)
    assert db.rows('SELECT * FROM jev_ledger')==ledger and len(calls)==n
    frozen=json.loads(db.one('SELECT snapshot FROM inbound_review_sets WHERE id=?',(rid,))['snapshot']);frozen['config']['criteria'][0]['name']='Tampered'
    db.execute('UPDATE inbound_review_sets SET snapshot=? WHERE id=?',(db.dumps(frozen),rid))
    with pytest.raises(Exception):service.start(rid,True)
    assert len(calls)==n
