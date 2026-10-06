import asyncio
import copy
import json
import pytest
from backend import db,engine,sheets
from backend.api import approve_criteria,approve,update_config,VersionBody
from backend.models import ConfigUpdate
from backend.errors import IntegrationError
from test_engine import PROFILE,CONFIG,file,read,fake_ai,database,approve_fixture

def ingest_job():
    asyncio.run(engine.ingest([file('a','CV1 CV','candidate_public'),file('j','IT-1 JD','job_public')],read,True))

def result(score=100,must='MET'):
    return {'score':score,'coverage':100,'must_complete':True,'must_passed':must=='MET','strategy_ids':['S1'],
        'strengths':['Python'],'gaps':[], 'assessments':[
            {'criterion_id':'C1','status':must,'evidence':[{'source_id':'a','quote':'Worked with Python'}],'explanation':'Direct evidence'},
            {'criterion_id':'C2','status':'MET','evidence':[],'explanation':'SQL'}]}

def completed(fake_ai):
    ingest_job();c=approve_fixture('IT-1');rid=engine.create_run('IT-1',c['version'])
    db.execute("UPDATE runs SET status='COMPLETED' WHERE id=?",(rid,))
    db.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?)',(rid,'CV1',db.dumps(result()),'COMPLETED',None))
    return rid

def test_criteria_must_be_approved_then_strategy_generated_then_final_approval(fake_ai):
    ingest_job();c=db.unpack(db.one('SELECT * FROM configs'))
    assert c['data']['strategies']==[] and not c['criteria_approved']
    with pytest.raises(IntegrationError):asyncio.run(engine.generate_strategies('IT-1',1,2))
    with pytest.raises(IntegrationError):approve('IT-1',VersionBody(version=1))
    c=approve_fixture('IT-1');assert c['version']==2
    changed=json.loads(c['data']);changed['criteria'][0]['type']='NICE'
    c=update_config('IT-1',ConfigUpdate(version=2,config=changed))
    assert not c['approved'] and not c['criteria_approved'] and not c['strategies_current']
    assert len(c['data']['strategies'])==2 # retain reviewable history
    with pytest.raises(IntegrationError):engine.create_run('IT-1',c['version'])

def test_strategy_edit_keeps_criteria_approval_but_requires_matching_review(fake_ai):
    ingest_job();c=approve_fixture('IT-1');changed=json.loads(c['data'])
    changed['strategies'][0]['description']='Manual recruiter strategy'
    c=update_config('IT-1',ConfigUpdate(version=c['version'],config=changed))
    assert c['criteria_approved'] and c['strategies_current'] and not c['approved']
    approve('IT-1',VersionBody(version=c['version']))
    assert engine.create_run('IT-1',c['version'])

def test_inflight_strategy_does_not_overwrite_concurrent_criteria_edit(fake_ai,monkeypatch):
    ingest_job();approve_criteria('IT-1',VersionBody(version=1))
    async def editing_ai(task,payload,schema,validate):
        c=db.unpack(db.one('SELECT * FROM configs'));c['data']['criteria'][0]['name']='Changed by recruiter'
        update_config('IT-1',ConfigUpdate(version=1,config=c['data']))
        return {'strategies':CONFIG['strategies']}
    monkeypatch.setattr(engine,'ai',editing_ai)
    with pytest.raises(IntegrationError):asyncio.run(engine.generate_strategies('IT-1',1,2))
    c=db.unpack(db.one('SELECT * FROM configs'))
    assert c['data']['criteria'][0]['name']=='Changed by recruiter' and c['data']['strategies']==[]

def test_must_filter_different_from_coverage_and_threshold():
    for status in ('PARTIAL','NOT_MET','UNKNOWN'):
        d=result(99,status)
        assert not engine.eligible(d,85,CONFIG)
    assert engine.eligible(result(),85,CONFIG)
    assert not engine.eligible(result(85),85,CONFIG)

def test_export_is_idempotent_and_contains_snapshot_sources_no_ai(fake_ai):
    rid=completed(fake_ai);calls=len(fake_ai)
    first=sheets.create_export(rid,85,10);again=sheets.create_export(rid,85,10)
    assert first['id']==again['id'] and len(fake_ai)==calls
    payload=json.loads(db.one('SELECT payload FROM sheet_exports')['payload'])
    assert payload['candidate_ids']==['CV1'] and payload['tabs']['Evidence'][1][6]=='a'
    assert 'Worked with Python' in sheets.csv_bytes(payload).decode('utf-8-sig')
    db.execute('UPDATE profiles SET revision=revision+1 WHERE kind=?',('candidate',))
    with pytest.raises(IntegrationError):sheets.create_export(rid,85,10)
    assert sheets.export_payload(rid,85,10,live=False)['candidate_ids']==['CV1']

def test_review_only_accepts_exact_ids_and_valid_feedback(fake_ai):
    rid=completed(fake_ai);payload=sheets.export_payload(rid,85,10)
    values=copy.deepcopy(payload['tabs']['Shortlist']);values[1][10:13]=[True,'GOOD','Verify scope']
    selected,feedback=sheets.parse_review(payload,values)
    assert selected==['CV1'] and feedback[0]['note']=='Verify scope'
    for invalid in ('CV999','CV1'):
        wrong=copy.deepcopy(values);wrong.append(copy.deepcopy(values[1]));wrong[-1][0]=invalid
        with pytest.raises(IntegrationError):sheets.parse_review(payload,wrong)
    values[1][11]='HIRE'
    with pytest.raises(IntegrationError):sheets.parse_review(payload,values)

def test_csv_formula_neutralized():
    data=sheets.csv_bytes({'tabs':{'Shortlist':[['Title'],[' =HYPERLINK("evil")']]}}).decode('utf-8-sig')
    assert "' =HYPERLINK" in data

def test_sheet_publish_raw_values_and_no_second_creation(fake_ai,monkeypatch):
    rid=completed(fake_ai);eid=sheets.create_export(rid,85,10)['id'];calls=[]
    async def request(method,path,body=None,retry=True):
        calls.append((method,path,body))
        if path=='':return {'spreadsheetId':'sheet1','spreadsheetUrl':'https://docs.google.com/spreadsheets/d/sheet1/edit'}
        return {}
    monkeypatch.setattr(sheets,'request',request)
    asyncio.run(sheets.publish(eid));asyncio.run(sheets.publish(eid))
    assert sum(path=='' for _,path,_ in calls)==1
    assert calls[1][2]['valueInputOption']=='RAW'
    assert db.one('SELECT status FROM sheet_exports')['status']=='COMPLETED'

def test_sheet_creation_ambiguity_does_not_autoretry_after_restart(fake_ai,monkeypatch):
    rid=completed(fake_ai);eid=sheets.create_export(rid,85,10)['id']
    async def request(*args,**kwargs):raise IntegrationError('timeout')
    monkeypatch.setattr(sheets,'request',request)
    with pytest.raises(IntegrationError):asyncio.run(sheets.publish(eid))
    db.init();assert db.one('SELECT status FROM sheet_exports')['status']=='CREATE_UNCERTAIN'

def test_feedback_import_idempotent_and_does_not_enqueue_ai(fake_ai,monkeypatch):
    rid=completed(fake_ai);eid=sheets.create_export(rid,85,10)['id']
    db.execute("UPDATE sheet_exports SET status='COMPLETED',spreadsheet_id='sheet1' WHERE id=?",(eid,))
    payload=json.loads(db.one('SELECT payload FROM sheet_exports')['payload']);values=payload['tabs']['Shortlist']
    values[1][10:13]=[True,'GOOD','Ask scope']
    async def request(*args,**kwargs):return {'values':values}
    monkeypatch.setattr(sheets,'request',request);tasks=len(db.rows('SELECT * FROM tasks'))
    one=asyncio.run(sheets.import_review(eid));two=asyncio.run(sheets.import_review(eid))
    assert one['selected_ids']==['CV1'] and one['feedback_imported']==1 and two['feedback_imported']==0
    assert len(db.rows('SELECT * FROM tasks'))==tasks and len(db.rows('SELECT * FROM feedback'))==1

def test_topk_orders_score_then_id_and_excludes_partial_must(fake_ai):
    rid=completed(fake_ai)
    for eid,score,must in [('CV2',90,'MET'),('CV3',95,'MET'),('CV4',99,'PARTIAL')]:
        db.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)',(eid,'candidate',1,db.dumps(PROFILE),'hash','READY',db.now()))
        db.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?)',(rid,eid,db.dumps(result(score,must)),'COMPLETED',None))
    run=db.one('SELECT * FROM runs WHERE id=?',(rid,));snap=json.loads(run['snapshot'])
    for eid in ('CV2','CV3','CV4'):snap['candidates'][eid]=copy.deepcopy(snap['candidates']['CV1'])
    db.execute('UPDATE runs SET snapshot=? WHERE id=?',(db.dumps(snap),rid))
    assert sheets.export_payload(rid,85,2)['candidate_ids']==['CV1','CV3']
    assert sheets.export_payload(rid,85,10)['candidate_ids']==['CV1','CV3','CV2']

def test_definite_sheet_permission_failure_can_retry_without_uncertain_creation(fake_ai,monkeypatch):
    rid=completed(fake_ai);eid=sheets.create_export(rid,85,10)['id']
    async def request(*args,**kwargs):raise sheets.SheetHTTPError(403)
    monkeypatch.setattr(sheets,'request',request)
    with pytest.raises(IntegrationError):asyncio.run(sheets.publish(eid))
    assert db.one('SELECT status FROM sheet_exports')['status']=='FAILED'
