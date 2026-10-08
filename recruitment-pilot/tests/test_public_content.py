import asyncio,json,copy
import pytest
from backend import db,engine,content_fetch,public_assessment,people_search
from backend.errors import IntegrationError
from test_engine import database,fake_ai
from test_public_assessment import setup
from test_exa import mock_api

def test_content_cache_snapshot_empty_pool_and_restart(fake_ai,monkeypatch):
    setup(fake_ai);monkeypatch.setenv('EXA_API_KEY','test-key')
    calls=mock_api(monkeypatch,[(200,{'results':[{'url':'https://example.com/person','text':'Worked with Python\n\n## Social\nReposted this SQL article'}],
        'statuses':[{'id':'https://example.com/person','status':'success','source':'cached'}],'costDollars':{'total':.001}})])
    row=content_fetch.create('S',['https://example.com/person'])
    assert content_fetch.create('S',['https://example.com/person'])['id']==row['id']
    asyncio.run(content_fetch.run(row['id']));r=content_fetch.detail(row['id'])
    assert r['status']=='COMPLETED' and r['known_cost_dollars']==.001
    assert calls[0][1]['json']=={'urls':['https://example.com/person'],'text':True}
    assert r['results'][0]['paragraphs'][1]['kind']=='context_only'
    assert not db.rows("SELECT id FROM profiles WHERE kind='candidate'")
    db.init();assert content_fetch.detail(row['id'])['results']==r['results']
    newer=content_fetch.create('S',['https://example.com/person'],True)
    assert newer['id']!=row['id'] and content_fetch.detail(row['id'])['results']==r['results']

def test_statuses_partial_and_invalid_selection(fake_ai,monkeypatch):
    setup(fake_ai);monkeypatch.setenv('EXA_API_KEY','test-key')
    response=json.loads(db.one('SELECT response FROM web_searches')['response'])
    response['results'].append({'title':'Missing','url':'https://example.com/missing','highlights':[]})
    db.execute('UPDATE web_searches SET response=?',(db.dumps(response),))
    mock_api(monkeypatch,[(200,{'results':[{'url':'https://example.com/person','text':'Worked with Python'}],
        'statuses':[{'id':'https://example.com/person','status':'success'}, {'id':'https://example.com/missing','status':'error','error':{'tag':'not_found'}}]})])
    for urls in ([],['https://example.com/person']*2,['http://localhost/a']):
        with pytest.raises(IntegrationError):content_fetch.create('S',urls)
    f=content_fetch.create('S',['https://example.com/person','https://example.com/missing']);asyncio.run(content_fetch.run(f['id']))
    assert content_fetch.detail(f['id'])['status']=='PARTIAL'
    assert content_fetch.detail(f['id'])['has_unreported_cost']

def test_precise_quotes_and_context_rejected(fake_ai,monkeypatch):
    version=setup(fake_ai);monkeypatch.setenv('EXA_API_KEY','test-key')
    mock_api(monkeypatch,[(200,{'results':[{'url':'https://example.com/person','text':'Worked with Python\n\n## Social\nReposted SQL'}],
        'statuses':[{'id':'https://example.com/person','status':'success'}]})])
    f=content_fetch.create('S',['https://example.com/person']);asyncio.run(content_fetch.run(f['id']))
    row=public_assessment.create('IT-1',version,'S',content_fetch_id=f['id'])
    snap=row['input_snapshot'];catalog=snap['sources'][0]['evidence'];ref=next(iter(catalog))
    answer={'items':[{'candidate_id':'PUB001','assessments':[
        {'criterion_id':'C1','status':'MET','evidence_refs':[ref],'explanation':'Python','quotes':[{'paragraph_id':ref,'quote':'Python'}]},
        {'criterion_id':'C2','status':'UNKNOWN','evidence_refs':[],'explanation':'Unknown','quotes':[]}]}]}
    public_assessment.validate(answer,snap)
    from backend.search_schema import PublicJudgeBatch,PublicCriterion
    assert 'e' not in PublicCriterion.model_json_schema()['properties']
    parsed=PublicJudgeBatch.model_validate({'items':[{'id':'PUB001','a':[{'c':'C1','s':'MET','why':'Python','q':[{'paragraph_id':ref,'quote':'Python'}]}]}]}).model_dump()
    assert parsed['items'][0]['assessments'][0]['evidence_refs']==[ref]
    wrong_case=copy.deepcopy(answer);wrong_case['items'][0]['assessments'][0]['quotes'][0]['quote']='python'
    with pytest.raises(IntegrationError) as failure:public_assessment.validate(wrong_case,snap)
    assert failure.value.feedback['invalid_quotes'][0]['case_sensitive_alternatives'][0]['exact_quote']=='Python'
    bad=copy.deepcopy(answer);bad['items'][0]['assessments'][0]['quotes'][0]['quote']='invented'
    with pytest.raises(IntegrationError):public_assessment.validate(bad,snap)
    context=list(snap['sources'][0]['paragraph_kinds'])[1];bad=copy.deepcopy(answer);bad['items'][0]['assessments'][0].update(evidence_refs=[context],quotes=[{'paragraph_id':context,'quote':'SQL'}])
    with pytest.raises(IntegrationError):public_assessment.validate(bad,snap)

def test_paragraph_refs_identify_their_source_without_changing_text():
    from backend.content_fetch import blocks
    parts=blocks('Direct Python experience\n\n## Social\nReposted this SQL article')
    first=public_assessment.paragraph_catalog(parts,'PUB001')
    second=public_assessment.paragraph_catalog(parts,'PUB002')
    assert not set(first['evidence'])&set(second['evidence'])
    assert list(first['evidence'].values())==list(second['evidence'].values())==['Direct Python experience']
    assert all(k.startswith('PUB001.') for k in first['paragraph_kinds'])

def test_evidence_selection_is_lossless_bounded_and_source_local():
    from backend.content_fetch import blocks
    from backend.search_schema import PublicEvidenceBatch
    text='Python expertise and personal project delivery. '*35
    catalog=public_assessment.selection_catalog(blocks(text),'PUB001')
    assert ''.join(catalog['evidence'].values())==text
    assert all(len(t)<=350 for t in catalog['evidence'].values())
    ref=next(iter(catalog['evidence']))
    snap={'content_fetch_id':'F','criteria':[{'id':'C1'}],'sources':[{'id':'PUB001',**catalog}]}
    parsed=PublicEvidenceBatch.model_validate({'items':[{'id':'PUB001','a':[{'c':'C1','s':'MET','why':'Python','q':[ref]}]}]}).model_dump()
    public_assessment.validate_selection(parsed,snap)
    assert parsed['items'][0]['assessments'][0]['quotes']==[{'paragraph_id':ref,'quote':catalog['evidence'][ref]}]
    bad=copy.deepcopy(parsed);bad['items'][0]['assessments'][0]['evidence_refs']=['PUB002.P001.S001']
    with pytest.raises(IntegrationError):public_assessment.validate_selection(bad,snap)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PublicEvidenceBatch.model_validate({'items':[{'id':'PUB001','a':[{'c':'C1','s':'NOT_MET','why':'Thiếu dữ liệu','q':[]}]}]})

def test_common_must_preserves_alternatives_and_blocks_legacy():
    data={'criteria':[{'id':'C1','type':'MUST','enabled':True}], 'public_musts':[{'criterion_id':'C1','query':'At least 1 year UI/UX or web design'}]}
    query,final=people_search.composed_query(data,{'exa_query':'Sitecore developers'},'VIETNAM')
    assert '1 year UI/UX or web design' in final and 'Vietnam' in final
    assert 'Sitecore developers' in query
    with pytest.raises(IntegrationError):people_search.must_block({**data,'public_musts':[]})
    with pytest.raises(IntegrationError):people_search.composed_query(data,{'exa_query':'a'*1490},'VIETNAM')

def test_content_get_routes_are_before_spa(fake_ai):
    from fastapi.testclient import TestClient
    from backend.api import app
    setup(fake_ai)
    client=TestClient(app)
    response=client.get('/api/v1/people-searches/S/content-fetches')
    assert response.status_code==200 and response.json()==[]

def test_chunks_lossless_and_professional_budget():
    text='Python '+''.join('long professional experience '+str(i)+' ' for i in range(100))
    parts=content_fetch.blocks(text)
    assert ''.join(p['text'] for p in parts)==text
    assert max(len(p['text']) for p in parts)<=3000
    people_search.validate_query('Experience managing token budgets and compute budget optimization')
    with pytest.raises(IntegrationError):people_search.validate_query('Candidate salary USD 5000')

def test_running_assessment_does_not_call_again(fake_ai,monkeypatch):
    version=setup(fake_ai)
    row=public_assessment.create('IT-1',version,'S')
    db.execute("UPDATE public_assessments SET status='RUNNING' WHERE id=?",(row['id'],))
    async def forbidden(*args,**kwargs):raise AssertionError('Already claimed run must not call AI')
    monkeypatch.setattr(engine,'ai',forbidden)
    asyncio.run(public_assessment.assess(row['id']))
    assert public_assessment.detail(row['id'])['attempts']==0


def test_profile_pdf_creates_new_snapshot_and_never_pool(fake_ai,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app
    from backend import integrations
    setup(fake_ai);monkeypatch.setenv('EXA_API_KEY','test-key')
    mock_api(monkeypatch,[(200,{'results':[{'url':'https://example.com/person','text':'Worked with Python'}]})])
    f=content_fetch.create('S',['https://example.com/person']);asyncio.run(content_fetch.run(f['id']))
    before=content_fetch.detail(f['id'])['results']
    monkeypatch.setattr(integrations,'extract',lambda raw,mime:'Verified-user PDF text with Python')
    client=TestClient(app,headers={'x-pilot-request':'1'})
    response=client.post('/api/v1/content-fetches/'+f['id']+'/profile-document',data={'source_url':'https://example.com/person'},files={'file':('profile.pdf',b'%PDF-test','application/pdf')})
    assert response.status_code==201
    assert response.json()['id']!=f['id']
    assert response.json()['results'][0]['provider_source']=='user-provided-pdf'
    assert content_fetch.detail(f['id'])['results']==before
    assert not db.rows("SELECT id FROM profiles WHERE kind='candidate'")
    again=client.post('/api/v1/content-fetches/'+f['id']+'/profile-document',data={'source_url':'https://example.com/person'},files={'file':('profile.pdf',b'%PDF-test','application/pdf')})
    assert again.json()['id']==response.json()['id']
