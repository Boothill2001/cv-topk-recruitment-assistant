import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from backend import db,engine
from backend.api import app
from backend.integrations import IntegrationError

PROFILE={'summary':'Tóm tắt','title':'Engineer','skills':['Python'],'domains':[],
         'experience_summary':'Có kinh nghiệm','years_experience':None,'signals':[], 'unknowns':[], 'contradictions':[], 'facts':[{'id':'F1','field':'skills','value':'Python','numeric_value':None,'polarity':'POSITIVE','evidence':[{'source_id':'a','quote':'Worked with Python'}]}], '_meta':{'schema_version':2}}
CONFIG={'criteria':[{'id':'C1','name':'Python','type':'MUST','enabled':True,'description':'Dùng Python'},
                    {'id':'C2','name':'SQL','type':'NICE','enabled':True,'description':'SQL'}],
        'strategies':[{'id':'S1','name':'Engineering','description':'Engineering','enabled':True},
                      {'id':'S2','name':'Platform','description':'Platform engineering','enabled':True}], 'review_notes':[]}
for c in CONFIG['criteria']:c['rule']={'mode':'ALL','predicates':[{'field':'skills','operator':'any_of','values':[c['name']],'number':None,'weight':1}]}
for strategy in CONFIG['strategies']:strategy['exa_query']='Python engineering professional profiles';strategy['rule']={'mode':'ANY','predicates':[{'field':'skills','operator':'any_of','values':['Python'],'number':None,'weight':1}]}

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'RUNTIME',tmp_path);db.init();db.set_setting('ai_check',{'ok':True,'model':'deepseek-flash'})

def file(id,name,group,text='Worked with Python every day, remote only.',modified='1'):
    return {'id':id,'name':name,'group':group,'mimeType':'text/plain','modifiedTime':modified,'webViewLink':'https://drive.google.com/file/d/'+id+'/view','text':text}

async def read(f):return f['text']

@pytest.fixture
def fake_ai(monkeypatch):
    calls=[]
    async def fake(task,payload,schema,validate=None):
        calls.append(task)
        if task=='profile':
            value=json.loads(json.dumps(PROFILE));value.pop('_meta',None)
            value['facts'][0]['evidence']=[{'source_id':payload['sources'][0]['source_id'],'quote':payload['sources'][0]['text'][:20]}]
        elif task=='criteria':value={k:json.loads(json.dumps(CONFIG[k])) for k in ('criteria','review_notes')}
        elif task=='strategies':value={'strategies':json.loads(json.dumps(CONFIG['strategies'])),'public_musts':[{'criterion_id':'C1','query':'Python experience'}]}
        else:raise AssertionError('Unexpected '+task)
        if validate:validate(value)
        return schema.model_validate(value).model_dump()
    monkeypatch.setattr(engine,'ai',fake);return calls

def approve_fixture(job_id):
    from backend.api import approve_criteria,approve,VersionBody
    c=db.one('SELECT * FROM configs WHERE job_id=?',(job_id,))
    approve_criteria(job_id,VersionBody(version=c['version']))
    asyncio.run(engine.generate_strategies(job_id,c['version'],2))
    c=db.one('SELECT * FROM configs WHERE job_id=?',(job_id,))
    approve(job_id,VersionBody(version=c['version']))
    return c

def test_ids_are_preserved_and_names_do_not_join():
    assert engine.identity('CV427 Anonymous.pdf','candidate_public')=='CV427'
    assert engine.identity('IT-595c Job.docx','job_public')=='IT-595c'
    assert engine.identity('John Doe.pdf','candidate_public') is None

def test_incremental_unchanged_no_ai_and_changed_note_invalidates(fake_ai):
    fs=[file('a','CV427 CV.pdf','candidate_public'),file('b','CV427 note','candidate_private'),file('j','IT-599 JD','job_public')]
    asyncio.run(engine.ingest(fs,read,True));n=len(fake_ai)
    config=approve_fixture('IT-599');n=len(fake_ai)
    rid=engine.create_run('IT-599',config['version']);db.execute("UPDATE runs SET status='COMPLETED'")
    asyncio.run(engine.ingest(fs,read,True));assert len(fake_ai)==n
    fs[1]['text']='Only onsite in Hanoi, updated preference.';fs[1]['modifiedTime']='2'
    asyncio.run(engine.ingest(fs,read,True))
    assert db.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='STALE'
    assert db.one("SELECT revision FROM profiles WHERE id='CV427'")['revision']==2
    history=[json.loads(r['data']) for r in db.rows("SELECT data FROM source_revisions WHERE file_id='b' ORDER BY revision")]
    assert len(history)==2 and history[0]['text']!=history[1]['text']

def test_deleted_public_no_longer_ready(fake_ai):
    fs=[file('a','CV427 CV','candidate_public')]
    asyncio.run(engine.ingest(fs,read,True));asyncio.run(engine.ingest([],read,True))
    assert db.one('SELECT status FROM profiles')['status']=='NEEDS_SOURCE'
    assert db.one('SELECT available FROM files')['available']==0

def test_unknown_not_zero_or_eligible():
    e={'assessments':[{'criterion_id':'C1','status':'UNKNOWN'}, {'criterion_id':'C2','status':'MET'}]}
    result=engine.calculate(CONFIG,e)
    assert result['score']==25 and result['coverage']==25 and not result['must_complete']
    assert not engine.eligible(result,85)
    assert not engine.eligible({'score':85,'must_complete':True},85)

def test_evidence_rejects_fabrication_wrong_source_and_accepts_whitespace():
    src=[{'source_id':'a','text':'Python\n every day'}]
    engine.validate_evidence([{'source_id':'a','quote':'Python every day'}],src)
    quoted={'source_id':'a','quote':'Python every day'}
    engine.validate_evidence([quoted],[{'source_id':'a','text':'Python\neveryday'}])
    assert quoted['quote']=='Python\neveryday'
    for e in [{'source_id':'wrong','quote':'Python every day'},{'source_id':'a','quote':'SQL every day'}]:
        with pytest.raises(IntegrationError):engine.validate_evidence([e],src)

def test_duplicate_id_not_arbitrarily_selected(fake_ai):
    asyncio.run(engine.ingest([file('a','CV1 CV','candidate_public'),file('b','CV1 CV2','candidate_public')],read,True))
    assert all(f['status']=='DUPLICATE' for f in db.rows('SELECT status FROM files'))

def test_config_sensitive_constraints_rejected():
    config=json.loads(json.dumps(CONFIG));config['criteria'][0]['name']='Salary expectation'
    with pytest.raises(IntegrationError):engine.validate_config(config)
    config['criteria'][0]['name']='English language programming'
    engine.validate_config(config)

def test_selection_limit_exact_set_and_stale(fake_ai):
    fs=[file('a','CV1 CV','candidate_public'),file('j','IT-1 JD','job_public')]
    asyncio.run(engine.ingest(fs,read,True));c=approve_fixture('IT-1')
    r=engine.create_run('IT-1',c['version']);db.execute("UPDATE runs SET status='COMPLETED'")
    evaluation={'score':100,'must_complete':True,'assessments':[{'criterion_id':'C1','status':'MET'},{'criterion_id':'C2','status':'MET'}]}
    db.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?)',(r,'CV1',db.dumps(evaluation),'COMPLETED',None))
    assert engine.create_report(r,['CV1'],5,85)
    for ids in [['CV2'],['CV1','CV1']]:
        with pytest.raises(IntegrationError):engine.create_report(r,ids,5,85)
    with pytest.raises(IntegrationError):engine.create_report(r,['CV'+str(i) for i in range(6)],5,85)
    db.execute('UPDATE configs SET version=2,approved=0')
    with pytest.raises(IntegrationError):engine.create_report(r,['CV1'],5,85)

def test_queue_idempotence_restart_preserves_history():
    assert engine.enqueue('sync',{})==engine.enqueue('sync',{})
    db.execute("UPDATE tasks SET status='RUNNING'")
    db.init();assert db.one('SELECT status FROM tasks')['status']=='INTERRUPTED'
    assert engine.enqueue('sync',{})!=1

def test_local_api_csrf_and_no_secret():
    c=TestClient(app)
    assert c.post('/api/bootstrap').status_code==403
    assert c.post('/api/bootstrap',headers={'X-Pilot-Request':'1','Origin':'https://evil.example'}).status_code==403
    status=c.get('/api/status').json()
    assert 'DEEPSEEK_API_KEY' not in json.dumps(status)


@pytest.mark.parametrize('text',['Tạo động lực cho đội ngũ','Kỹ năng lãnh đạo, quản lý đội ngũ, tạo động lực','Motivating employees','Motivate team'])
def test_team_motivation_is_professional(text):
    import copy
    config=copy.deepcopy(CONFIG);config['criteria'][0]['name']=text;config['criteria'][0]['description']=text
    engine.validate_config(config,False)

@pytest.mark.parametrize('text',['Động lực chuyển việc của ứng viên','Candidate motivation to change jobs','Tạo động lực cho đội ngũ; salary expectation','Motivating employees; nationality'])
def test_personal_motivation_stays_excluded(text):
    import copy
    config=copy.deepcopy(CONFIG);config['criteria'][0]['name']=text
    with pytest.raises(IntegrationError):engine.validate_config(config,False)
