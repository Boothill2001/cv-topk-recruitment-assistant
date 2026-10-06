import asyncio,json
import pytest
from fastapi.testclient import TestClient
from backend import db,engine,single_cv,search_orchestration as service,ai_runtime,integrations
from backend.api import app
from backend.errors import IntegrationError
from test_engine import database,fake_ai,file,read
from test_batch_search import setup,batch

def test_direct_cv_only_reuses_profile_and_preserves_scope(fake_ai,monkeypatch):
    version=setup(fake_ai,3)
    # A direct assessment needs confirmed criteria, not discovery strategies.
    db.execute("UPDATE configs SET approved=0,strategy_hash=NULL WHERE job_id='IT-1'")
    n=len(fake_ai)
    t=single_cv.register('IT-1',version,b'%PDF-one','Worked with Python on a new project.','new.pdf')
    assert single_cv.register('IT-1',version,b'%PDF-one','Worked with Python on a new project.','new.pdf')['ticket_id']==t['ticket_id']
    asyncio.run(single_cv.process(t['ticket_id']))
    assert fake_ai[n:]==['profile']
    t=single_cv.detail(t['ticket_id']);sid=t['search_id'];d=service.detail(sid)
    assert d['scope']=='single-cv' and list(d['group'])==[t['candidate_id']]
    calls=[]
    async def ai(task,payload,schema,validate,**kw):
        calls.append(payload)
        assert [p['candidate_id'] for p in payload['candidates']]==[t['candidate_id']]
        kw['on_attempt']();value=batch(payload);validate(value);return value
    monkeypatch.setattr(ai_runtime,'ai',ai)
    asyncio.run(service.assess(sid));assert len(calls)==1
    assert service.detail(sid)['results'][0]['candidate_id']==t['candidate_id']
    asyncio.run(engine.ingest([file('a0','CV0 CV','candidate_public'),file('a1','CV1 CV','candidate_public'),file('a2','CV2 CV','candidate_public'),file('new','CV999 CV','candidate_public'),file('j','IT-1 JD','job_public')],read,True))
    assert service.detail(sid)['is_current']
    assert db.one('SELECT status FROM runs WHERE id=?',(d['retrieval_run_id'],))['status']=='COMPLETED'
    # Unrelated pool revisions never invalidate this direct assessment.
    db.execute("UPDATE profiles SET revision=revision+1 WHERE id='CV0'")
    assert service.detail(sid)['is_current']
    n=len(fake_ai);asyncio.run(single_cv.process(t['ticket_id']))
    assert len(fake_ai)==n and single_cv.detail(t['ticket_id'])['search_id']==sid
    db.execute("UPDATE profiles SET revision=revision+1 WHERE id=?",(t['candidate_id'],))
    assert not service.detail(sid)['is_current']

def test_upload_validation_approval_and_duplicate_tasks(fake_ai,monkeypatch):
    version=setup(fake_ai,1);monkeypatch.setattr(integrations,'extract',lambda *a:'Worked with Python on a new project.')
    with TestClient(app) as c:
        headers={'X-Pilot-Request':'1'};path='/api/v1/jobs/IT-1/cv-assessments'
        def upload(content=b'%PDF-one'):return c.post(path,data={'config_version':version},files={'file':('new.pdf',content,'application/pdf')},headers=headers)
        assert upload(b'not-pdf').status_code==400
        assert upload(b'%PDF-'+b'x'*(15*1024*1024)).status_code==413
        def scan(*a):raise IntegrationError('NEEDS_OCR: scan')
        monkeypatch.setattr(integrations,'extract',scan);assert upload().status_code==422
        db.execute("UPDATE configs SET criteria_approved=0 WHERE job_id='IT-1'")
        assert upload().status_code==409
        assert db.one("SELECT COUNT(*) n FROM tasks WHERE kind='single_cv'")['n']==0
        db.execute("UPDATE configs SET criteria_approved=1 WHERE job_id='IT-1'")
        monkeypatch.setattr(integrations,'extract',lambda *a:'Worked with Python on a new project.')
        a=upload();b=upload();assert a.status_code==202 and a.json()['ticket_id']==b.json()['ticket_id']
        eid=a.json()['candidate_id']
        assert c.get('/api/v1/uploaded-cvs/'+eid+'/source').content==b'%PDF-one'
        assert db.one("SELECT COUNT(*) n FROM tasks WHERE kind='single_cv'")['n']==1

def test_retry_single_search_never_searches_pool(fake_ai):
    version=setup(fake_ai,3)
    t=single_cv.register('IT-1',version,b'%PDF-one','Worked with Python on a new project.','new.pdf')
    asyncio.run(single_cv.process(t['ticket_id']));sid=single_cv.detail(t['ticket_id'])['search_id']
    db.execute("UPDATE searches SET status='FAILED',attempts=1 WHERE id=?",(sid,))
    with TestClient(app) as c:
        r=c.post('/api/v1/searches/'+sid+'/retry',headers={'X-Pilot-Request':'1'})
        assert r.status_code==202 and r.json()['search_id']==sid
        assert service.detail(sid)['input_count']==1
        db.execute('UPDATE searches SET attempts=3 WHERE id=?',(sid,))
        assert c.post('/api/v1/searches/'+sid+'/retry',headers={'X-Pilot-Request':'1'}).status_code==409

def test_failed_ingestion_retry_deduplicates_and_local_source_survives_sync(fake_ai):
    version=setup(fake_ai,1)
    t=single_cv.register('IT-1',version,b'%PDF-one','Worked with Python on a new project.','new.pdf')
    db.execute("UPDATE tasks SET status='FAILED',error='API timeout' WHERE id=?",(t['task_id'],))
    a=single_cv.retry(t['ticket_id']);b=single_cv.retry(t['ticket_id'])
    assert a['task_id']==b['task_id'] and a['task_id']!=t['task_id']
    asyncio.run(engine.ingest([file('a0','CV0 CV','candidate_public'),file('j','IT-1 JD','job_public')],read,True))
    assert db.one('SELECT available FROM files WHERE id=?',('local:'+t['candidate_id'],))['available']==1
