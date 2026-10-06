import asyncio,json
import pytest
from backend import db,engine,public_assessment as service
from backend.errors import IntegrationError
from test_engine import database,fake_ai,file,read,approve_fixture

def setup(fake_ai):
    asyncio.run(engine.ingest([file('j','IT-1 JD','job_public'),file('n','IT-1 note','job_private','SECRET PRIVATE NOTE')],read,True))
    c=approve_fixture('IT-1')
    now=db.now()
    response={'results':[{'title':'Synthetic professional','url':'https://example.com/person','highlights':['Worked with Python']}],'retrieved_at':now}
    db.execute('INSERT INTO web_searches(id,fingerprint,query,status,created,updated,mode,response) VALUES(?,?,?,?,?,?,?,?)',('S','f','Python engineer','COMPLETED',now,now,'people',db.dumps(response)))
    return c['version']

def answer():
    return {'items':[{'candidate_id':'PUB001','assessments':[
        {'criterion_id':'C1','status':'MET','evidence_refs':['E1'],'explanation':'Có Python'},
        {'criterion_id':'C2','status':'UNKNOWN','evidence_refs':[],'explanation':'Chưa có SQL'}]}]}

def test_cache_snapshot_batch_scoring_and_restart(fake_ai,monkeypatch):
    version=setup(fake_ai);row=service.create('IT-1',version,'S')
    assert service.create('IT-1',version,'S')['id']==row['id']
    snap=json.loads(db.one('SELECT snapshot FROM public_assessments')['snapshot'])
    assert 'SECRET PRIVATE NOTE' not in db.dumps(snap['public_jd'])
    calls=[]
    async def ai(task,payload,schema,validate,**kw):
        calls.append(task);kw['on_attempt']();kw['on_usage']({'total_tokens':123},'COMPLETED')
        assert 'be_rank' not in db.dumps(payload)
        result=schema.model_validate(answer()).model_dump();validate(result);return result
    monkeypatch.setattr(engine,'ai',ai)
    asyncio.run(service.assess(row['id']));asyncio.run(service.assess(row['id']))
    result=service.detail(row['id']);assert calls==['public_assessment']
    assert result['results'][0]['score']==75 and result['results'][0]['coverage']==75
    assert result['results'][0]['assessments'][1]['status']=='UNKNOWN'
    assert result['results'][0]['assessments'][0]['evidence'][0]['quote']=='Worked with Python'
    db.init();assert service.detail(row['id'])['status']=='COMPLETED'
    assert service.create('IT-1',version,'S')['id']==row['id']
    assert len(db.rows("SELECT id FROM tasks WHERE kind='public_assessment'"))==1

@pytest.mark.parametrize('mutate',[
    lambda a:a['items'].append(a['items'][0]),
    lambda a:a['items'][0].update(candidate_id='PUB002'),
    lambda a:a['items'][0]['assessments'][0].update(evidence_refs=['E99']),
    lambda a:a['items'][0]['assessments'][0].update(criterion_id='C99'),
    lambda a:a['items'][0]['assessments'][1].update(evidence_refs=['E1']),
])
def test_reject_ids_evidence_and_unknown(fake_ai,mutate):
    row=service.create('IT-1',setup(fake_ai),'S')
    snap=json.loads(db.one('SELECT snapshot FROM public_assessments')['snapshot'])
    result=answer();mutate(result)
    with pytest.raises(IntegrationError):service.validate(result,snap)

def test_gate_staleness_retry_budget_and_interrupt(fake_ai):
    version=setup(fake_ai)
    db.execute("UPDATE configs SET criteria_approved=0 WHERE job_id='IT-1'")
    with pytest.raises(IntegrationError):service.create('IT-1',version,'S')
    db.execute("UPDATE configs SET criteria_approved=1 WHERE job_id='IT-1'")
    db.execute("UPDATE web_searches SET mode='web'")
    with pytest.raises(IntegrationError):service.create('IT-1',version,'S')
    db.execute("UPDATE web_searches SET mode='people'")
    row=service.create('IT-1',version,'S')
    db.execute("UPDATE public_assessments SET status='RUNNING'");db.init()
    assert service.detail(row['id'])['status']=='INTERRUPTED'
    assert service.retry(row['id'])['status']=='PENDING'
    db.execute("UPDATE public_assessments SET status='FAILED',attempts=3")
    with pytest.raises(IntegrationError):service.retry(row['id'])
    db.execute("UPDATE configs SET version=version+1")
    assert not service.detail(row['id'])['is_current']

def test_unverified_model_budget_blocks_before_queue(fake_ai):
    version=setup(fake_ai);db.set_setting('model','unverified-model')
    with pytest.raises(IntegrationError):service.create('IT-1',version,'S')
    assert not db.rows('SELECT id FROM public_assessments')

def test_people_group_works_without_candidate_pool(fake_ai,monkeypatch):
    version=setup(fake_ai)
    from backend import people_search
    group={'job_id':'IT-1','is_current':True,'status':'COMPLETED','results':[
        {'title':'Synthetic public source','url':'https://example.com/person','highlights':['Worked with Python']}]}
    monkeypatch.setattr(people_search,'detail',lambda id:group)
    assert not db.rows("SELECT id FROM profiles WHERE kind='candidate'")
    row=service.create('IT-1',version,'GROUP')
    assert row['source_count']==1 and row['status']=='PENDING'
    assert service.create('IT-1',version,'GROUP')['id']==row['id']
    group['is_current']=False
    with pytest.raises(IntegrationError):service.create('IT-1',version,'OTHER')
