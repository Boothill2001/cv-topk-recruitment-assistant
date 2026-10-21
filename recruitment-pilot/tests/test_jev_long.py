import asyncio,json
import pytest,httpx
from backend import db,jev,jev_planner as planner,assessment_groups as groups,public_assessment
from backend.errors import IntegrationError
from test_engine import database,fake_ai
from test_jev import content,stub
from test_public_assessment import setup

@pytest.mark.parametrize('language',['English profile. ','Hồ sơ tiếng Việt. ','候选人工作经历。'])
def test_lossless_long_unicode_and_boundaries(language):
    text=language*4000
    source={'id':'PUB001','revision':'r','url':'https://example.com/p','raw_text':text,
            'evidence':{'PUB001.P1':text},'context':[]}
    criteria=[{'id':'C1','name':'At least 5 years specialised experience','rule':{'mode':'ALL'}}]
    requests,mapping=jev.evidence_requests(source,criteria)
    parts=planner.parts(source)
    assert ''.join(p['text'] for p in parts)==text
    assert parts[0]['start']==0 and parts[-1]['end']==len(text)
    assert all(a['end']==b['start'] for a,b in zip(parts,parts[1:]))
    assert len(mapping)==len(parts) and len(parts)>1
    assert all(jev.request_bound(p)[0]<=jev.CONTEXT and jev.request_bound(p)[1]<=jev.STATE_QUESTION for p in requests)
    assert all('full_source_text' not in p['state'] for p in requests)

@pytest.mark.parametrize('count',[10,20,43])
def test_preview_all_sources_no_provider(monkeypatch,count):
    calls=stub(monkeypatch)
    text='Personal experience with Python. '*1500
    sources=[{'id':f'PUB{i:03}','url':f'https://example.com/{i}','revision':str(i),
        'raw_text':text,'evidence':{f'PUB{i:03}.P1':text},'context':[]} for i in range(count)]
    result=jev.preview({'sources':sources,'criteria':[{'id':'C1','name':'Python','rule':{'mode':'ALL','predicates':[{'field':'skills','operator':'any_of','values':['Python']}]}}]})
    assert result['technical_fits'] and len(result['source_plans'])==count and not calls
    assert all(s['covered_characters']==len(text) for s in result['source_plans'])

def test_wrong_catalog_and_rule_rejected():
    with pytest.raises(IntegrationError):planner.parts({'id':'P','revision':'r','raw_text':'real','evidence':{'P.1':'invented'}})
    with pytest.raises(IntegrationError):planner.rubric([{'id':'C','rule':{'mode':'XOR'}}])
    atoms,layout=planner.atomic_criteria([{'id':'C','name':'Prose cannot be safely split'}])
    assert not atoms and layout['C']['requires_review']

def test_atomic_all_any_and_scoped_duration():
    criteria=[{'id':'C','name':'Specialised experience','rule':{'mode':'ALL','predicates':[
        {'field':'skills','operator':'all_of','values':['C#','SQL']},
        {'field':'years_experience','operator':'gte','number':3}]}}]
    atoms,layout=planner.atomic_criteria(criteria)
    assert len(atoms)==3 and atoms[-1]['component_predicate']['number']==3
    assert all(a['approved_criterion']==criteria[0] for a in atoms)
    assert planner.combine(['MET','MET'],'ALL')=='MET'
    assert planner.combine(['MET','UNKNOWN'],'ALL')=='PARTIAL'
    assert planner.combine(['MET','UNKNOWN'],'ANY')=='MET'
    assert planner.combine(['UNKNOWN','UNKNOWN'],'ANY')=='UNKNOWN'
    assert planner.combine(['NOT_MET','UNKNOWN'],'ALL')=='NOT_MET'

def test_all_evidence_distributed_across_parts(monkeypatch):
    first='Personal C# experience. '+('Neutral content. '*1000)
    last='Personal SQL experience.'
    source={'id':'PUB001','revision':'r','raw_text':first+last,
            'evidence':{'P1':first,'P2':last}}
    criteria=[{'id':'C','name':'C# and SQL','rule':{'mode':'ALL','predicates':[
        {'field':'skills','operator':'all_of','values':['C#','SQL']}]}}]
    async def execute(aid,source,phase,payload,**kwargs):
        answers={}
        for cid,q in payload['questions'].items():
            if phase.startswith('reduce:'):
                status='MET' if any(x['choice']=='MET' for x in payload['state']['validated_part_judgments']) else 'UNKNOWN'
            else:
                value=q['instructions']['criterion']['component_predicate']['values'][0]
                status='MET' if value in payload['state']['text'] else 'UNKNOWN'
            answers[cid]={'choice':status}
        return {'answers':answers}
    monkeypatch.setattr(jev,'execute',execute)
    result=asyncio.run(jev.judge_selected('A',source,criteria,{'C':{'P1':'PARTIAL','P2':'PARTIAL'}}))
    assert result['C']['choice']=='MET'
    assert len(result['C']['component_judgments'])==2

def test_plan_manifest_rejects_dependency_offset_or_hash_change():
    source={'id':'PUB001','revision':'r','raw_text':'Python experience','evidence':{'PUB001.P1':'Python experience'}}
    criteria=[{'id':'C1','name':'Python','rule':{'mode':'ANY','predicates':[{'field':'skills','operator':'equals','values':['python']}]}}]
    value=planner.manifest(source,criteria)
    assert value['rubric'][0]['components'][0]['predicate']==criteria[0]['rule']['predicates'][0]
    planner.validate_manifest(value,source,criteria)
    for key,bad in [('revision','other'),('content_hash','bad'),('parts',[])]:
        with pytest.raises(IntegrationError):planner.validate_manifest({**value,key:bad},source,criteria)

def test_budget_confirmation_no_provider_and_stale_write(fake_ai,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app
    setup(fake_ai);calls=stub(monkeypatch)
    client=TestClient(app,headers={'X-Pilot-Request':'1'})
    body={'limit_dollars':1,'expected_limit_dollars':.5,'confirm':True}
    response=client.put('/api/v1/jev-trial-budget',json=body)
    assert response.status_code==200 and response.json()['limit_dollars']==1
    assert client.put('/api/v1/jev-trial-budget',json=body).status_code==409
    assert not calls and not db.rows('SELECT id FROM tasks WHERE kind=\'assessment_group\'')

def test_budget_wait_resume_reuses_completed_steps(fake_ai,monkeypatch):
    row=content(setup(fake_ai));calls=stub(monkeypatch)
    source=row['sources'][0];snap=row['input_snapshot']
    first,_=jev.evidence_requests(source,snap['criteria'])
    asyncio.run(jev.execute(row['id'],source,'evidence:0',first[0]))
    monkeypatch.setattr(jev,'TRIAL_NANO',4200)
    asyncio.run(public_assessment.assess(row['id']))
    paused=public_assessment.detail(row['id'])
    assert paused['status']=='WAITING_BUDGET' and len(calls)==1 and paused['attempts']==0
    db.set_setting('jev_trial_limit_nano',500_000_000)
    asyncio.run(public_assessment.assess(row['id']))
    assert public_assessment.detail(row['id'])['status']=='COMPLETED' and len(calls)==2

def test_timeout_is_uncertain_never_replayed(fake_ai,monkeypatch):
    row=content(setup(fake_ai));calls=stub(monkeypatch)
    class Broken:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):calls.append('timeout');raise httpx.ReadTimeout('lost response')
    monkeypatch.setattr(jev.httpx,'AsyncClient',Broken)
    source=row['sources'][0];payload=jev.evidence_requests(source,row['input_snapshot']['criteria'])[0][0]
    for _ in range(2):
        with pytest.raises(IntegrationError):asyncio.run(jev.execute(row['id'],source,'evidence:0',payload))
    assert len(calls)==1 and jev.progress(row['id'])[0]['status']=='UNCERTAIN'

def test_continuation_keeps_parent_and_never_refetches(fake_ai,monkeypatch):
    from test_assessment_groups import prepare
    version,sources,exa,old_engine=prepare(fake_ai,monkeypatch,3)
    now=db.now();text='Worked with Python'
    items=[{**s,'status':'AVAILABLE','text':text,'paragraphs':[], 'revision':f'r{i}',
            'provider_source':'cached','fetched_at':now,'adapter_version':'test'} for i,s in enumerate(sources)]
    db.execute('INSERT INTO content_fetches VALUES(?,?,?,?,?,?,?,?,?,?)',('C','C','G',db.dumps({}),
        'COMPLETED',db.dumps({'results':items,'usage':[]}),0,None,now,now))
    parent=groups.create('G',version,'JEV');pid=parent['id'];value=groups.row(pid)
    snap=value['snapshot'];snap['adapter_version']='jev-evidence-1'
    data=value['data'];data['chunks']=[{'urls':[s['url'] for s in sources],'fetch_id':'C'}]
    data['inherited_results']=[{'url':sources[0]['url'],'candidate_id':'SRC0','title':'Saved result','score':0,
        'coverage':0,'assessments':[],'be_rank':0,'lane':'NEEDS_VERIFICATION','assessment_id':'old','child_source_id':'PUB001'}]
    db.execute("UPDATE assessment_groups SET status='PARTIAL',snapshot=?,data=? WHERE id=?",(db.dumps(snap),db.dumps(data),pid))
    before=db.one('SELECT * FROM assessment_groups WHERE id=?',(pid,))
    calls=stub(monkeypatch)
    pre=groups.continuation_preview(pid)
    assert pre['retained_sources']==1 and pre['remaining_sources']==2 and not calls
    follow=groups.continue_group(pid)
    assert groups.continue_group(pid)['id']==follow['id']
    def forbidden(*a,**k):raise AssertionError('No Exa/content refetch on continuation')
    monkeypatch.setattr(groups.content_fetch,'create',forbidden)
    asyncio.run(groups.run(follow['id']))
    r=groups.detail(follow['id'])
    assert r['status']=='COMPLETED' and r['assessed_sources']==3,(r['error'],r['unassessed_sources'])
    assert r['mixed_adapter_versions'] and r['parent_group_id']==pid and len(calls)==4
    assert db.one('SELECT * FROM assessment_groups WHERE id=?',(pid,))==before
    assert not exa and not old_engine
