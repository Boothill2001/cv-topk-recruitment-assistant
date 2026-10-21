import asyncio,json
from backend import db,engine,search_orchestration as service,internal_jev
from test_engine import database,fake_ai,file,read,approve_fixture
from test_jev import stub

def setup_pool(fake_ai):
    asyncio.run(engine.ingest([file('j','IT-1 JD','job_public'),
      file('a','CV1 CV','candidate_public','Worked with Python for 3 years.'),
      file('b','CV1 note','candidate_private','PRIVATE SECRET NOT FOR JEV')],read,True))
    return approve_fixture('IT-1')['version']

def test_internal_snapshot_privacy_preview_and_evidence(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);calls=stub(monkeypatch)
    preview=service.create('IT-1',version,10,assessment_engine='JEV',preview=True)
    assert preview['fits'] and preview['usage_estimate']['source_count']==1 and not calls
    assert not db.rows('SELECT * FROM searches')
    sid=service.create('IT-1',version,10,assessment_engine='JEV')
    snapshot=json.loads(db.one('SELECT snapshot FROM searches WHERE id=?',(sid,))['snapshot'])
    assert 'PRIVATE SECRET' not in db.dumps(snapshot['jev'])
    asyncio.run(service.assess(sid));result=service.detail(sid)
    assert result['status']=='COMPLETED',result
    assert result['assessment_engine']=='JEV' and result['results'][0]['candidate_id']=='CV1'
    evidence=result['results'][0]['assessments'][0]['evidence'][0]
    assert evidence['source_id']=='a' and evidence['revision']==1
    assert evidence['quote']=='Worked with Python for 3 years.'
    count=len(calls);asyncio.run(service.assess(sid));assert len(calls)==count
    assert service.create('IT-1',version,10,assessment_engine='JEV')==sid
    assert service.create('IT-1',version,10)!=sid
    from fastapi.testclient import TestClient
    from backend.api import app
    client=TestClient(app,headers={'X-Pilot-Request':'1'})
    original=db.dumps(result['results'])
    response=client.post('/api/v1/searches/'+sid+'/evidence-reviews',json={
      'candidate_id':'CV1','criterion_id':'C1','decision':'INCORRECT','note':'Cần kiểm tra ý nghĩa.'})
    assert response.status_code==200,response.text
    reviews=client.get('/api/v1/searches/'+sid+'/evidence-reviews').json()
    assert reviews[0]['details']['criterion_id']=='C1'
    assert not db.rows('SELECT * FROM labels')
    assert db.dumps(service.detail(sid)['results'])==original
    bad=client.post('/api/v1/searches/'+sid+'/evidence-reviews',json={
      'candidate_id':'CV999','criterion_id':'C1','decision':'CORRECT'})
    assert bad.status_code==422
    trace=client.get('/api/v1/searches/'+sid+'/trace')
    assert trace.status_code==200,trace.text
    assert 'PRIVATE SECRET' not in trace.text and 'secret-test-key' not in trace.text
    data=trace.json()
    assert len(data['provider_steps'])==2 and data['recruiter_reviews']
    assert data['input_snapshot']['sources'][0]['provenance'][evidence['paragraph_id']]['source_id']=='a'
    exported=client.get('/api/v1/searches/'+sid+'/trace-export')
    assert exported.status_code==200 and 'attachment' in exported.headers['content-disposition']
    assert len(calls)==count  # Reviews, trace and exports never call a provider.

def test_internal_unknown_and_missing_revision(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);calls=stub(monkeypatch,lambda *_:'NONE')
    sid=service.create('IT-1',version,10,assessment_engine='JEV')
    asyncio.run(service.assess(sid));result=service.detail(sid)
    assert all(a['status']=='UNKNOWN' for a in result['results'][0]['assessments'])
    assert len(calls)==1
    snap=json.loads(db.one('SELECT snapshot FROM searches WHERE id=?',(sid,))['snapshot'])
    db.execute("DELETE FROM source_revisions WHERE file_id='a'")
    rebuilt=internal_jev.build(snap)
    assert not rebuilt['sources'] and 'CV1' in rebuilt['missing']

def test_restart_reuses_completed_jev_steps(fake_ai,monkeypatch):
    version=setup_pool(fake_ai);calls=stub(monkeypatch)
    sid=service.create('IT-1',version,10,assessment_engine='JEV')
    row=db.one('SELECT snapshot FROM searches WHERE id=?',(sid,));snap=json.loads(row['snapshot'])
    from backend import jev
    requests,_=jev.evidence_requests(snap['jev']['sources'][0],snap['jev']['criteria'])
    asyncio.run(jev.execute(sid,snap['jev']['sources'][0],'evidence:0',requests[0]))
    db.execute("UPDATE searches SET status='RUNNING' WHERE id=?",(sid,))
    db.init()
    assert db.one('SELECT status FROM searches WHERE id=?',(sid,))['status']=='PENDING'
    asyncio.run(service.assess(sid))
    assert service.detail(sid)['status']=='COMPLETED'
    assert len(calls)==2  # One evidence call before restart, only judgment afterwards.
