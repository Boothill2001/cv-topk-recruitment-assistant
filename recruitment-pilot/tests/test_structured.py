import asyncio
import copy
import json
import pytest
from fastapi.testclient import TestClient
from backend import db,engine,retrieval,benchmark
from backend.api import app,feedback
from backend.models import FeedbackRequest
from test_engine import database,fake_ai,file,read,approve_fixture,CONFIG,PROFILE

def rule(field,values,mode='ALL'):
    return {'mode':mode,'predicates':[{'field':field,'operator':'any_of','values':values,'number':None,'weight':1}]}

def fact(field,value,negative=False):
    return {'id':value,'field':field,'value':value,'numeric_value':None,'polarity':'NEGATIVE' if negative else 'POSITIVE','evidence':[{'source_id':'a','quote':'Worked with Python'}]}

def config():
    c=copy.deepcopy(CONFIG);c['criteria']=c['criteria'][:1]
    c['policy']=retrieval.policy({});return c

def test_strategy_changes_topk_without_changing_technical_score():
    c=config();c['strategies'][0]['rule']=rule('domains',['fintech']);c['strategies'][1]['rule']=rule('domains',['banking'])
    a={'facts':[fact('skills','Python')]};b={'facts':[fact('skills','Python'),fact('domains','fintech')]}
    first=retrieval.evaluate(c,a);second=retrieval.evaluate(c,b)
    assert first['score']==second['score']==100
    assert second['retrieval_score']>first['retrieval_score'] and second['strategy_ids']==['S1']
    c['strategies'][0]['enabled']=False;c['strategies'][1]['enabled']=False
    assert retrieval.evaluate(c,a)['retrieval_score']==retrieval.evaluate(c,b)['retrieval_score']==100

def test_unknown_not_negative_and_policy_operator_gate_configurable():
    c=config();unknown=retrieval.evaluate(c,{'facts':[]});failed=retrieval.evaluate(c,{'facts':[fact('skills','Python',True)]})
    assert unknown['assessments'][0]['status']=='UNKNOWN' and unknown['lane']=='NEEDS_VERIFICATION'
    assert failed['assessments'][0]['status']=='NOT_MET' and failed['lane']=='BELOW_POLICY'
    passed=retrieval.evaluate(c,{'facts':[fact('skills','Python')]})
    c['policy']['threshold']=100
    assert retrieval.lane(passed,c)=='BELOW_POLICY'
    c['policy']['threshold_operator']='>='
    assert retrieval.lane(passed,c)=='QUALIFIED'
    c['policy'].update(gate='REVIEW_ONLY',threshold_enabled=False)
    assert retrieval.lane(failed,c)=='QUALIFIED' and retrieval.lane(unknown,c)=='NEEDS_VERIFICATION'

def test_partial_unknown_conjunction_still_needs_verification():
    c=config();c['criteria'][0]['rule']['predicates'].append(rule('domains',['fintech'])['predicates'][0])
    d=retrieval.evaluate(c,{'facts':[fact('skills','Python')]})
    assert d['assessments'][0]['status']=='PARTIAL' and not d['must_complete']
    c['policy'].update(gate='NO_CONFIRMED_FAILURE',threshold_enabled=False)
    assert retrieval.lane(d,c)=='NEEDS_VERIFICATION'

def test_absence_of_one_any_alternative_does_not_mean_not_met():
    c=config();c['criteria'][0]['rule']['predicates'].append(rule('skills',['SQL'])['predicates'][0]);c['criteria'][0]['rule']['mode']='ANY'
    d=retrieval.evaluate(c,{'facts':[fact('skills','Python',True)]})
    assert d['assessments'][0]['status']=='UNKNOWN'

def test_one_negative_anyof_value_does_not_reject_other_unknown_values():
    c=config();c['criteria'][0]['rule']=rule('skills',['Python','SQL'])
    d=retrieval.evaluate(c,{'facts':[fact('skills','Python',True)]})
    assert d['assessments'][0]['status']=='UNKNOWN'
    d=retrieval.evaluate(c,{'facts':[fact('skills','Python',True),fact('skills','SQL')]})
    assert d['assessments'][0]['status']=='MET'

def test_two_of_three_domains_is_not_one_of_three():
    c=config();c['criteria'][0]['rule']={'mode':'ALL','predicates':[{'field':'domains','operator':'at_least','values':['fintech','fmcg','digital-platform'],'number':2,'weight':1}]}
    one=retrieval.evaluate(c,{'facts':[fact('domains','fintech')]})
    assert one['assessments'][0]['status']=='PARTIAL' and one['lane']=='NEEDS_VERIFICATION'
    two=retrieval.evaluate(c,{'facts':[fact('domains','fintech'),fact('domains','fmcg')]})
    assert two['assessments'][0]['status']=='MET'

def test_new_jd_never_reads_unchanged_cv_or_calls_evaluation(fake_ai,monkeypatch):
    cv=file('a','CV1.pdf','candidate_public');jd=file('j','IT-1.pdf','job_public')
    asyncio.run(engine.ingest([cv,jd],read,True));c=approve_fixture('IT-1')
    before=len(fake_ai);reads=[]
    async def tracking(f):reads.append(f['id']);return f['text']
    asyncio.run(engine.ingest([cv,jd,file('k','IT-2.pdf','job_public')],tracking,True))
    assert reads==['k'] and fake_ai[before:]==['profile','criteria']
    def no_sources(*args):raise AssertionError('Round 1 read full source text')
    monkeypatch.setattr(engine,'sources',no_sources)
    async def no_ai(*args,**kwargs):raise AssertionError('Round 1 called AI')
    monkeypatch.setattr(engine,'ai',no_ai)
    rid=engine.create_run('IT-1',c['version'])
    assert db.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='COMPLETED'
    snap=json.loads(db.one('SELECT snapshot FROM runs WHERE id=?',(rid,))['snapshot'])
    assert 'text' not in snap['candidates']['CV1']['sources'][0]
    assert db.setting('retrieval_metrics:'+rid)['llm_calls']==0

def test_round2_accepts_exact_human_selected_unknown_ids(fake_ai):
    asyncio.run(engine.ingest([file('a','CV1.pdf','candidate_public'),file('j','IT-1.pdf','job_public')],read,True));c=approve_fixture('IT-1')
    rid=engine.create_run('IT-1',c['version'])
    assert engine.create_report(rid,['CV1'],20,85)
    with pytest.raises(engine.IntegrationError):engine.create_report(rid,['CV999'],20,0)

def test_unknown_and_unjudged_labels_not_negative(fake_ai):
    assert benchmark.evaluate()['status']=='INSUFFICIENT_LABELS'
    asyncio.run(engine.ingest([file('a','CV1.pdf','candidate_public'),file('j','IT-1.pdf','job_public')],read,True));c=approve_fixture('IT-1');rid=engine.create_run('IT-1',c['version'])
    feedback(FeedbackRequest(run_id=rid,candidate_id='CV1',decision='UNCERTAIN',note='Need evidence'))
    result=benchmark.evaluate();assert result['labeled_pairs']==0 and result['uncertain_or_stale_or_missing_pairs']==1

def test_legacy_matching_cannot_resume():
    db.execute('INSERT INTO runs(id,snapshot) VALUES(?,?)',('old','{}'))
    with pytest.raises(engine.IntegrationError):asyncio.run(engine.match('old'))

def test_all_strategies_can_be_disabled_for_real_baseline_run(fake_ai):
    from backend.api import update_config,approve,VersionBody
    from backend.models import ConfigUpdate
    asyncio.run(engine.ingest([file('a','CV1.pdf','candidate_public'),file('j','IT-1.pdf','job_public')],read,True));c=approve_fixture('IT-1')
    data=json.loads(c['data'])
    for s in data['strategies']:s['enabled']=False
    c=update_config('IT-1',ConfigUpdate(version=c['version'],config=data));approve('IT-1',VersionBody(version=c['version']))
    rid=engine.create_run('IT-1',c['version']);result=db.unpack(db.one('SELECT data FROM evaluations WHERE run_id=?',(rid,)))['data']
    assert result['retrieval_score']==result['score'] and result['strategy_details']==[]

def test_comparison_cannot_join_two_quotes_into_invented_evidence():
    source=[{'source_id':'a','text':'Python\nSQL','quotes':['Python','SQL']}]
    with pytest.raises(engine.IntegrationError):engine.validate_evidence([{'source_id':'a','quote':'Python SQL'}],source)

def test_upload_text_jd_is_queued_and_identical_upload_is_cached(fake_ai):
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject,NameObject,DictionaryObject
    pdf=BytesIO();writer=PdfWriter();page=writer.add_blank_page(600,800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 50 750 Td (Senior Python engineer. Five years of backend experience.) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream);writer.write(pdf)
    client=TestClient(app);headers={'X-Pilot-Request':'1'}
    r=client.post('/api/jobs/upload',headers=headers,files={'file':('JD.pdf',pdf.getvalue(),'application/pdf')},data={'job_id':'IT-901'})
    assert r.status_code==200 and r.json()['status']=='PENDING'
    before=len(fake_ai)
    repeat=client.post('/api/jobs/upload',headers=headers,files={'file':('JD.pdf',pdf.getvalue(),'application/pdf')},data={'job_id':'IT-901'})
    assert repeat.json()['status']=='UNCHANGED' and len(fake_ai)==before
    # A Drive-backed ID opens the existing job without replacing its source,
    # invalidating approvals or triggering another ingestion/LLM call.
    db.execute("UPDATE files SET id='drive-jd' WHERE id='local:IT-901'")
    before_tasks=len(db.rows('SELECT * FROM tasks'))
    existing=client.post('/api/jobs/upload',headers=headers,files={'file':('IT-901 JD.pdf',pdf.getvalue(),'application/pdf')},data={'private_note':'New note must not be silently saved'})
    assert existing.status_code==200 and existing.json()['status']=='EXISTING'
    assert existing.json()['job_id']=='IT-901' and 'chưa được lưu' in existing.json()['message']
    assert not db.one("SELECT id FROM files WHERE id='local:IT-901'")
    assert len(db.rows('SELECT * FROM tasks'))==before_tasks and len(fake_ai)==before

def test_upload_no_mock_pdf_scan_and_duplicate_identity(fake_ai):
    from io import BytesIO
    from pypdf import PdfWriter
    pdf=BytesIO();writer=PdfWriter();writer.add_blank_page(100,100);writer.write(pdf)
    c=TestClient(app)
    r=c.post('/api/jobs/upload',headers={'X-Pilot-Request':'1'},files={'file':('scan.pdf',pdf.getvalue(),'application/pdf')},data={'job_id':'IT-900'})
    assert r.status_code==200 and r.json()['status']=='NEEDS_OCR'
    assert db.one("SELECT status FROM profiles WHERE id='IT-900'")['status']=='NEEDS_SOURCE'
    bad=c.post('/api/jobs/upload',headers={'X-Pilot-Request':'1'},files={'file':('fake.pdf',b'not pdf','application/pdf')})
    assert bad.status_code==400

def test_prepare_job_recovery_is_job_only_and_idempotent(fake_ai):
    asyncio.run(engine.ingest([file('j','IT-1.pdf','job_public')],read,True))
    db.execute("UPDATE profiles SET status='PARSE_FAILED' WHERE kind='job' AND id='IT-1'")
    client=TestClient(app);headers={'X-Pilot-Request':'1'}
    first=client.post('/api/jobs/IT-1/prepare',headers=headers)
    second=client.post('/api/jobs/IT-1/prepare',headers=headers)
    assert first.status_code==200 and first.json()==second.json()
    task=db.one('SELECT * FROM tasks WHERE id=?',(first.json()['task_id'],))
    assert task['kind']=='normalize' and json.loads(task['payload'])=={'kind':'job','id':'IT-1'}
    assert client.post('/api/jobs/IT-999/prepare',headers=headers).status_code==404

def test_round2_requests_reuse_report_and_queue_and_recover_failed(fake_ai):
    asyncio.run(engine.ingest([file('a','CV1.pdf','candidate_public'),file('j','IT-1.pdf','job_public')],read,True))
    c=approve_fixture('IT-1');rid=engine.create_run('IT-1',c['version'])
    first=engine.create_report(rid,['CV1'],5,85)
    assert engine.create_report(rid,['CV1'],10,70)==first
    assert len(db.rows("SELECT * FROM tasks WHERE kind='compare'"))==1
    db.execute("UPDATE reports SET status='COMPLETED' WHERE id=?",(first,))
    assert engine.create_report(rid,['CV1'],5,85)==first
    assert len(db.rows("SELECT * FROM tasks WHERE kind='compare'"))==1
    db.execute("UPDATE reports SET status='FAILED' WHERE id=?",(first,))
    db.execute("UPDATE tasks SET status='FAILED' WHERE kind='compare'")
    assert engine.create_report(rid,['CV1'],5,85)==first
    assert db.one('SELECT status FROM reports WHERE id=?',(first,))['status']=='PENDING'

def test_config_read_marks_outdated_jd_without_discarding_draft(fake_ai):
    from backend.api import get_config
    asyncio.run(engine.ingest([file('j','IT-1.pdf','job_public')],read,True))
    before=get_config('IT-1');assert before['input_current']
    db.execute("UPDATE profiles SET revision=revision+1 WHERE id='IT-1' AND kind='job'")
    after=get_config('IT-1')
    assert not after['input_current'] and before['data']==after['data']
