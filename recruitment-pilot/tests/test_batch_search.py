import asyncio,copy,json
import pytest
from fastapi.testclient import TestClient
from backend import db,engine,retrieval,search_orchestration as service,ai_assessment,ai_runtime
from backend.api import app
from backend.errors import IntegrationError
from backend.search_schema import BatchAssessment
from test_engine import database,fake_ai,approve_fixture,file,read

def setup(fake_ai,count=30):
    fs=[file('a'+str(i),'CV'+str(i)+' CV','candidate_public') for i in range(count)]
    fs.append(file('j','IT-1 JD','job_public'))
    asyncio.run(engine.ingest(fs,read,True))
    c=approve_fixture('IT-1')
    return c['version']

def batch(payload):
    return {'items':[{'candidate_id':p['candidate_id'],'assessments':[
        {'criterion_id':c['id'],'status':'MET' if c['id']=='C1' else 'UNKNOWN',
         'evidence_refs':['E1'] if c['id']=='C1' else [],
         'explanation':'Có bằng chứng' if c['id']=='C1' else 'Thiếu thông tin','questions':[]}
        for c in payload['criteria']], 'strengths':['Python'],'gaps':['SQL chưa rõ'],'recommendation':'Cần xác minh SQL'}
        for p in payload['candidates']]}

def test_top20_one_batch_no_cv_reads_display_limits_and_idempotence(fake_ai,monkeypatch):
    version=setup(fake_ai);ingestion=len(fake_ai)
    monkeypatch.setattr(engine,'sources',lambda *a:(_ for _ in ()).throw(AssertionError('No full source read')))
    calls=[]
    async def ai(task,payload,schema,validate,**kw):
        assert task=='batch_assessment';calls.append(payload)
        assert len(payload['candidates'])==20
        assert not any(k in json.dumps(payload) for k in ('be_rank','be_evaluation','retrieval_score'))
        kw['on_attempt']();result=BatchAssessment.model_validate(batch(payload)).model_dump();validate(result)
        kw['on_usage']({'total_tokens':123},'COMPLETED');return result
    monkeypatch.setattr(ai_runtime,'ai',ai)
    sid=service.create('IT-1',version);assert service.create('IT-1',version)==sid
    assert len(fake_ai)==ingestion
    asyncio.run(service.assess(sid));assert len(calls)==1
    d=service.detail(sid);assert d['input_count']==20 and len(d['results'])==20
    assert all(i['lane']=='QUALIFIED' or i['lane']=='NEEDS_VERIFICATION' or i['lane']=='BELOW_POLICY' for i in d['results'])
    assert all(i['score']==75 and i['coverage']==75 for i in d['results'])
    with TestClient(app) as client:
        ordered=client.get('/api/v1/searches/'+sid+'/results?limit=10').json()['results']
        for n in (6,7,8):
            assert client.get('/api/v1/searches/'+sid+f'/results?limit={n}').json()['results']==ordered[:n]
        assert client.post('/api/v1/jobs/IT-1/searches',json={'config_version':version},headers={'X-Pilot-Request':'1'}).json()['search_id']==sid
    assert len(calls)==1
    assert d['usage'][0]['tokens']['total_tokens']==123
    assert db.one('SELECT COUNT(*) n FROM search_assessments')['n']==20

def test_small_pool_unknown_and_input_changes(fake_ai,monkeypatch):
    version=setup(fake_ai,3);sid=service.create('IT-1',version)
    async def ai(task,payload,schema,validate,**kw):
        kw['on_attempt']();r=batch(payload)
        for p in r['items']:
            for a in p['assessments']:a.update(status='UNKNOWN',evidence_refs=[])
        validate(r);return r
    monkeypatch.setattr(ai_runtime,'ai',ai)
    asyncio.run(service.assess(sid));d=service.detail(sid)
    assert len(d['results'])==3
    assert all(i['coverage']==0 and i['assessments'][0]['status']=='UNKNOWN' for i in d['results'])
    db.execute("UPDATE profiles SET revision=revision+1 WHERE id='CV0'")
    assert not service.detail(sid)['is_current'] and service.detail(sid)['status']=='STALE'

@pytest.mark.parametrize('failure',['candidate_missing','candidate_duplicate','criterion_missing','criterion_unknown','quote','cross_candidate','no_evidence'])
def test_strict_sets_and_evidence(fake_ai,failure):
    version=setup(fake_ai,2);sid=service.create('IT-1',version)
    snap=json.loads(db.one('SELECT snapshot FROM searches WHERE id=?',(sid,))['snapshot'])
    payload,sources=ai_assessment.build(snap);r=batch(payload)
    if failure=='candidate_missing':r['items'].pop()
    if failure=='candidate_duplicate':r['items'][1]=copy.deepcopy(r['items'][0])
    if failure=='criterion_missing':r['items'][0]['assessments'].pop()
    if failure=='criterion_unknown':r['items'][0]['assessments'][0]['criterion_id']='NO_SUCH'
    if failure=='quote':r['items'][0]['assessments'][0]['evidence_refs']=['NO_SUCH_QUOTE']
    if failure=='cross_candidate':sources['CV0'][0]['source_id']='a1'
    if failure=='no_evidence':r['items'][0]['assessments'][0]['evidence_refs']=[]
    with pytest.raises(IntegrationError):ai_assessment.validate(r,payload,sources)

def test_aliases_and_all_skills_are_distinct():
    assert retrieval.canonical('Go')==retrieval.canonical('Golang')
    assert retrieval.canonical('Node.js')==retrieval.canonical('NodeJS')
    assert retrieval.canonical('Java')!=retrieval.canonical('JavaScript')
    evidence=[{'source_id':'a','quote':'React TypeScript multinational'}]
    facts=[{'field':'skills','value':v,'evidence':evidence} for v in ('React','TypeScript')]
    facts.append({'field':'company_context','value':'multinational','evidence':evidence})
    p={'field':'skills','operator':'at_least','values':['React','Redux','TypeScript'],'number':3,'weight':1}
    assert retrieval.assess({'mode':'ALL','predicates':[p]},facts)['status']=='PARTIAL'
    assert retrieval.assess({'mode':'ALL','predicates':[{**p,'field':'domains','operator':'any_of','values':['adtech'],'number':None}]},facts)['status']=='UNKNOWN'

def test_budget_rejected_before_call_and_attempts_survive_restart(fake_ai,monkeypatch):
    v=setup(fake_ai,2);sid=service.create('IT-1',v)
    db.set_setting('assessment_budgets',{'deepseek/deepseek-flash':{'context_chars':1,'output_tokens':12000}})
    async def forbidden(*a,**kw):raise AssertionError('Preflight should stop')
    monkeypatch.setattr(ai_runtime,'ai',forbidden)
    with pytest.raises(IntegrationError):asyncio.run(service.assess(sid))
    assert service.detail(sid)['attempts']==0 and service.detail(sid)['status']=='FAILED'
    db.set_setting('assessment_budgets',{})
    db.execute("UPDATE searches SET status='RUNNING',attempts=2 WHERE id=?",(sid,))
    db.init();assert service.detail(sid)['status']=='INTERRUPTED'
    assert service.create('IT-1',v)==sid
    assert db.one('SELECT attempts FROM searches WHERE id=?',(sid,))['attempts']==2


def test_missing_skill_cannot_be_not_met(fake_ai):
    v=setup(fake_ai,1);sid=service.create('IT-1',v)
    snap=json.loads(db.one('SELECT snapshot FROM searches WHERE id=?',(sid,))['snapshot'])
    payload,sources=ai_assessment.build(snap);r=batch(payload)
    a=r['items'][0]['assessments'][1];a.update(status='NOT_MET',evidence_refs=['E1'])
    with pytest.raises(IntegrationError,match='NOT_MET'):ai_assessment.validate(r,payload,sources)

def test_rule_review_proposes_versioned_all_and_removes_false_proxy():
    from backend.rule_review import propose
    cfg={'criteria':[{'id':'C1','name':'React & Redux & TypeScript','description':'Frontend','rule':{'mode':'ALL','predicates':[{'field':'skills','operator':'any_of','values':['React','Redux','TypeScript'],'number':None}]}},
                     {'id':'C2','name':'AdTech high-load','description':'AdTech','rule':{'mode':'ANY','predicates':[{'field':'company_context','operator':'any_of','values':['multinational'],'number':None}]}}]}
    original=copy.deepcopy(cfg);p=propose(cfg)
    assert cfg==original
    assert len(p['changes'])==2
    assert p['config']['criteria'][0]['rule']['predicates'][0]['number']==3
    assert p['config']['criteria'][1]['rule'] is None

def test_successful_response_survives_restart_and_export_includes_verification(fake_ai,monkeypatch):
    from backend import sheets,search_benchmark
    v=setup(fake_ai,3);sid=service.create('IT-1',v)
    snap=json.loads(db.one('SELECT snapshot FROM searches WHERE id=?',(sid,))['snapshot'])
    payload,sources=ai_assessment.build(snap);response=batch(payload);ai_assessment.validate(response,payload,sources)
    db.set_setting('search_response:'+sid,response)
    db.execute("UPDATE searches SET attempts=1,status='RUNNING' WHERE id=?",(sid,));db.init()
    assert service.create('IT-1',v)==sid
    async def forbidden(*a,**kw):raise AssertionError('Validated cached response must not call AI')
    monkeypatch.setattr(ai_runtime,'ai',forbidden)
    asyncio.run(service.assess(sid));d=service.detail(sid)
    p=sheets.export_payload(d['result_run_id'],85,2,False)
    assert len(p['tabs']['Shortlist'])==3 # headers + 2, even though nobody scores above 85.
    bench=search_benchmark.evaluate(db.one('SELECT * FROM searches WHERE id=?',(sid,)))
    assert bench['status']=='INSUFFICIENT_LABELS'
    assert bench['retrieval']['recall_at_input_count'] is None
    eid=d['results'][0]['candidate_id']
    db.execute('INSERT INTO labels(job_id,candidate_id,decision,note,profile_revision,job_revision,created) VALUES(?,?,?,?,?,?,?)',
               ('IT-1',eid,'GOOD','explicit test label',snap['group'][eid]['revision'],snap['job']['revision'],db.now()))
    bench=search_benchmark.evaluate(db.one('SELECT * FROM searches WHERE id=?',(sid,)))
    assert bench['retrieval']['recall_at_input_count']==1 and bench['ai_ranking']['judged_at_10']==1

def test_unknown_provider_model_requires_explicit_budget_before_transmission(fake_ai):
    setup(fake_ai,1);db.set_setting('provider','anthropic');db.set_setting('model','unknown-model')
    with pytest.raises(IntegrationError,match='budget'):ai_assessment.preflight({'candidates':[{}],'criteria':[{}]})

def test_max_three_attempts_total_even_when_resumed(fake_ai,monkeypatch):
    v=setup(fake_ai,1);sid=service.create('IT-1',v)
    db.execute('UPDATE searches SET attempts=2 WHERE id=?',(sid,))
    calls=[]
    class Adapter:
        async def generate(self,*args):calls.append(1);raise IntegrationError('Invalid JSON/evidence')
    monkeypatch.setattr(ai_runtime,'adapter',lambda p:Adapter())
    monkeypatch.setattr(ai_runtime,'key',lambda p:'test-key')
    with pytest.raises(IntegrationError):asyncio.run(service.assess(sid))
    assert len(calls)==1 and service.detail(sid)['attempts']==3
    assert service.create('IT-1',v)==sid
    assert db.one("SELECT COUNT(*) n FROM tasks WHERE kind='batch_assessment' AND status='PENDING'")['n']==1


def test_changed_profile_outside_top20_updates_run_but_reuses_same_assessment(fake_ai,monkeypatch):
    v=setup(fake_ai,30);calls=[]
    async def ai(task,payload,schema,validate,**kw):
        calls.append(1);kw['on_attempt']();r=batch(payload);validate(r);return r
    monkeypatch.setattr(ai_runtime,'ai',ai)
    first=service.create('IT-1',v);asyncio.run(service.assess(first))
    snap=json.loads(db.one('SELECT snapshot FROM searches WHERE id=?',(first,))['snapshot'])
    outside=next(p for p in db.rows("SELECT * FROM profiles WHERE kind='candidate'") if p['id'] not in snap['group'])
    db.execute('UPDATE profiles SET revision=revision+1 WHERE id=? AND kind=?',(outside['id'],'candidate'))
    engine.persist_facts(outside['id'],'candidate',outside['revision']+1,json.loads(outside['data']),outside['source_hash'])
    second=service.create('IT-1',v);assert first!=second
    asyncio.run(service.assess(second))
    assert len(calls)==1 and service.detail(second)['is_current']
    assert not service.detail(first)['is_current']


def test_compact_wire_preserves_every_fact_and_exact_quote(fake_ai):
    from backend.search_schema import JudgeBatch
    v=setup(fake_ai,2);sid=service.create('IT-1',v)
    snap=json.loads(db.one('SELECT snapshot FROM searches WHERE id=?',(sid,))['snapshot'])
    payload,sources=ai_assessment.build(snap);wire=ai_assessment.wire_payload(payload)
    for original,packed in zip(payload['candidates'],wire['candidates']):
        assert packed['id']==original['candidate_id']
        assert sum(map(len,packed['facts'].values()))==len(original['facts'])
        assert packed['evidence']=={e['id']:e['quote'] for e in original['evidence']}
        for f in original['facts']:
            assert [f['value'],f.get('numeric_value'),f.get('polarity','POSITIVE'),f['evidence_refs']] in packed['facts'][f['field']]
    rows={'items':[{'id':p['candidate_id'],'a':[{'c':c['id'],'s':'UNKNOWN','e':[],'why':'Chưa có bằng chứng'} for c in payload['criteria']]} for p in payload['candidates']]}
    expanded=ai_assessment.expand_judge(JudgeBatch.model_validate(rows).model_dump(),payload)
    ai_assessment.validate(expanded,payload,sources)
    assert all(a['status']=='UNKNOWN' and a['questions'] for p in expanded['items'] for a in p['assessments'])
    with pytest.raises(Exception):JudgeBatch.model_validate({'items':[{'id':'CV1','a':[{'c':'C1','s':'MET','e':['E1'],'why':'x'*141}]}]})
