import json
import pytest
from fastapi.testclient import TestClient
from backend import db,online,engine
from backend.api import app
from test_engine import database, fake_ai, file, read
import asyncio

@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv('PILOT_MODE','ONLINE_PILOT')
    monkeypatch.setenv('PILOT_ORIGIN','https://pilot.example')
    monkeypatch.setenv('PILOT_RELEASE','test-release')
    monkeypatch.setenv('PILOT_USERS_JSON',json.dumps({n:{'role':r,'password_hash':online.password_hash('test-password-only')} for n,r in [('tri','admin'),('dat','recruiter')]}))
    return TestClient(app,base_url='https://pilot.example')

def sign_in(web,name='dat'):
    r=web.post('/api/auth/login',json={'username':name,'password':'test-password-only'},headers={'origin':'https://pilot.example'})
    assert r.status_code==200
    assert 'Secure' in r.headers['set-cookie'] and 'HttpOnly' in r.headers['set-cookie']
    return {'origin':'https://pilot.example','x-csrf-token':r.json()['csrf']}

def test_session_permissions_and_direct_exports(web):
    for url in ['/api/status','/api/files','/api/local-source/JD1','/api/v1/public-assessments/a/export','/api/v1/pipeline-runs/a/export']:
        assert web.get(url).status_code==401
    assert web.get('/api/health').json()=={'ok':True}
    h=sign_in(web)
    assert web.get('/api/status').status_code==200
    for url in ['/api/profiles/candidate','/api/prompts','/api/pilot/audit','/docs','/openapi.json']:
        assert web.get(url).status_code==403
    for url in ['/api/sync','/api/connections/google','/api/bootstrap','/api/v1/searches','/api/v1/content-fetches/a/profile-document']:
        assert web.post(url,headers=h,json={}).status_code==403
    assert web.put('/api/pilot/settings',headers=h,json={'provider':'deepseek','model':'x'}).status_code==403
    assert web.post('/api/auth/logout',headers=h).status_code==200
    assert web.get('/api/status').status_code==401

def test_csrf_host_and_invalid_json(web):
    h=sign_in(web)
    assert web.post('/api/auth/logout',headers={'origin':'https://evil.example'}).status_code==403
    assert web.post('/api/auth/logout',headers={'origin':'https://pilot.example'}).status_code==403
    assert web.get('/api/status',headers={'host':'evil.example'}).status_code==403
    assert web.post('/api/v1/public-assessment-preview',headers={**h,'content-type':'application/json'},content='{').status_code==400
    assert web.post('/api/v1/public-assessment-preview',headers=h,json={'assessment_engine':'JEV'}).status_code==403
    assert web.get('/api/v1/public-assessment-preview?assessment_engine=JEV').status_code==403

def test_bodyless_prepare_with_json_content_type(web,monkeypatch):
    h=sign_in(web)
    monkeypatch.setattr(engine,'sources',lambda *a: ['saved-public-jd'])
    queued=[]
    monkeypatch.setattr(engine,'enqueue',lambda kind,payload: queued.append((kind,payload)) or 123)
    response=web.post('/api/jobs/JD_UPLOAD/prepare',headers={**h,'content-type':'application/json'})
    assert response.status_code==200
    assert response.json()=={'task_id':123}
    assert queued==[('normalize',{'kind':'job','id':'JD_UPLOAD'})]

def test_admin_settings_no_provider_call_and_credential_rotation(web,monkeypatch):
    h=sign_in(web,'tri')
    monkeypatch.setattr('backend.ai_runtime.check_ai',lambda *a,**k:pytest.fail('must not call provider'))
    assert web.put('/api/pilot/settings',headers=h,json={'provider':'deepseek','model':'deepseek-flash'}).status_code==200
    assert db.setting('model')=='deepseek-flash'
    assert web.get('/api/pilot/audit').status_code==200
    users=online.accounts();users['tri']['password_hash']=online.password_hash('changed-password')
    monkeypatch.setenv('PILOT_USERS_JSON',json.dumps(users))
    assert web.get('/api/status').status_code==401

def test_throttle_and_expiry(web):
    for _ in range(5):assert web.post('/api/auth/login',headers={'origin':'https://pilot.example'},json={'username':'dat','password':'wrong'}).status_code==401
    assert web.post('/api/auth/login',headers={'origin':'https://pilot.example'},json={'username':'dat','password':'test-password-only'}).status_code==429
    db.execute('DELETE FROM pilot_login_attempts')
    sign_in(web);db.execute('UPDATE pilot_sessions SET expires=0')
    assert web.get('/api/status').status_code==401

def test_three_public_jds_feedback_and_audit(web):
    h=sign_in(web)
    for id,title in [('JD1','Sitecore Developer'),('JD2','Phó Tổng Giám đốc'),('JD3','B2B Sales')]:
        db.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)',(id,'job',1,db.dumps({'title':title}),'hash','READY',db.now()))
        r=web.post('/api/pilot/feedback',headers=h,json={'job_id':id,'description':'Cần nguồn sát MUST hơn','expected':'Giữ số năm','quality':'Chờ review'})
        assert r.status_code==201
    assert len(web.get('/api/pilot/feedback').json())==3
    assert web.post('/api/pilot/feedback',headers=h,json={'job_id':'missing','description':'test'}).status_code==404
    assert web.post('/api/pilot/feedback',headers=h,json={'job_id':'JD1','run_id':'wrong','description':'test'}).status_code==400
    assert db.one('SELECT COUNT(*) n FROM pilot_audit')['n']>=3

def test_restart_stops_uncertain_work_and_blocks_paid_requests(web):
    h=sign_in(web,'tri')
    token=online.actor.set({'username':'tri','request_id':'req-test'})
    try:tid=engine.enqueue('web_search',{'search_id':'fixture'})
    finally:online.actor.reset(token)
    assert db.one('SELECT * FROM pilot_task_actors WHERE task_id=?',(tid,))['username']=='tri'
    db.execute("UPDATE tasks SET status='RUNNING' WHERE id=?",(tid,))
    online.interrupt_online()
    assert db.one('SELECT status FROM tasks WHERE id=?',(tid,))['status']=='INTERRUPTED'
    assert web.post('/api/v1/web-searches',headers=h,json={'query':'test'}).status_code==409
    assert web.post(f'/api/pilot/uncertain/{tid}/resolve',headers=h,json={'action':'CANCEL','checked_provider':False}).status_code==400
    assert web.post(f'/api/pilot/uncertain/{tid}/resolve',headers=h,json={'action':'CANCEL','checked_provider':True}).status_code==200
    assert db.one('SELECT status FROM tasks WHERE id=?',(tid,))['status']=='CANCELLED'

def test_no_private_upload(web):
    h=sign_in(web)
    assert web.post('/api/jobs/upload',headers=h,data={'private_note':'private'},files={'file':('jd.pdf',b'%PDF-test','application/pdf')}).status_code==403

def test_online_configuration_fail_closed(monkeypatch):
    monkeypatch.setenv('PILOT_MODE','unexpected')
    with pytest.raises(RuntimeError):online.enabled()
    monkeypatch.setenv('PILOT_MODE','ONLINE_PILOT');monkeypatch.setenv('PILOT_ORIGIN','http://not-secure')
    with pytest.raises(RuntimeError):online.configure()

@pytest.mark.parametrize('job_id,title', [
    ('JD601', 'Sitecore Developer'),
    ('JD602', 'Phó Tổng Giám đốc'),
    ('JD603', 'Senior Accountant'),
])
def test_public_jd_guidance_and_explicit_approval(web, fake_ai, monkeypatch, job_id, title):
    # Fixture AI only: verifies API sequencing, not provider/search quality.
    asyncio.run(engine.ingest([file('source-'+job_id, job_id+' '+title,
                                    'job_public')], read, True))
    asyncio.run(engine.propose(job_id))
    h=sign_in(web)
    config=web.get('/api/jobs/'+job_id+'/config').json()
    before=list(fake_ai)
    url='/api/v1/jobs/'+job_id+'/scouting-guidance'
    guidance=web.get(url).json()
    body={'version':guidance['current']['version'], 'content':'Ưu tiên kinh nghiệm thực tế; giữ mọi MUST.'}
    assert web.put(url, headers=h, json=body).status_code==200
    assert web.put(url, headers=h, json=body).status_code==409
    assert web.get('/api/jobs/'+job_id+'/config').json()['version']==config['version']
    assert web.post('/api/jobs/'+job_id+'/strategies', headers=h,
                    json={'version':config['version'],'count':2}).status_code==409
    approved=web.post('/api/jobs/'+job_id+'/criteria/approve',headers=h,
                      json={'version':config['version']})
    assert approved.status_code==200 and approved.json()['criteria_approved']
    assert fake_ai==before
    assert web.post('/api/jobs/'+job_id+'/strategies',headers=h,
                    json={'version':config['version'],'count':2,'guidance_version':1}).status_code==200
    assert fake_ai==before  # Accepted task; no worker/provider run in this test.
    from backend import scouting_guidance, exa_search
    from test_exa import mock_api
    original_ai=engine.ai
    async def guided_fixture(task,payload,schema,validate=None,**kwargs):
        assert 'Ưu tiên kinh nghiệm thực tế' in kwargs.get('prompt_override','')
        return await original_ai(task,payload,schema,validate)
    monkeypatch.setattr(engine,'ai',guided_fixture)
    asyncio.run(engine.generate_strategies(job_id, config['version'], 2,
        scouting_guidance.capture(job_id, 1, 'strategies')))
    current=web.get('/api/jobs/'+job_id+'/config').json()
    assert not current['approved']
    assert web.post('/api/jobs/'+job_id+'/approve',headers=h,
                    json={'version':current['version']}).status_code==200
    monkeypatch.setenv('EXA_API_KEY','fixture-only')
    calls=mock_api(monkeypatch,[(200,{'requestId':'fixture-'+job_id,
        'costDollars':{'total':0},'results':[{'title':'Fixture '+title,
        'url':'https://linkedin.com/in/fixture-'+job_id.lower(),
        'highlights':['Synthetic fixture; not a real candidate.']} ]})])
    created=web.post('/api/v1/jobs/'+job_id+'/people-searches',headers=h,
        json={'config_version':current['version'],'strategy_ids':['S1']})
    assert created.status_code==202
    group=created.json()
    asyncio.run(exa_search.run(group['items'][0]['search']['id']))
    detail=web.get('/api/v1/people-searches/'+group['id']).json()
    assert detail['status']=='COMPLETED' and len(detail['results'])==1
    trace=web.get('/api/v1/pipeline-runs/by-origin/people-search/'+group['id'])
    assert trace.status_code==200
    nodes={node['node_id'] for node in trace.json()['attempts']}
    assert 'search-snapshot' in nodes and 'discovered-sources' in nodes
    assert any(node.startswith('exa-search:') for node in nodes)
    assert web.get('/api/v1/pipeline-runs/'+trace.json()['id']+'/export').status_code==200
    assert web.post('/api/pilot/feedback',headers=h,json={'job_id':job_id,
        'run_id':group['id'],'description':'Fixture browser/API acceptance'}).status_code==201
    assert len(calls)==1  # History, trace/export and feedback must not search again.
