import asyncio,json,sqlite3
import httpx,pytest
from fastapi.testclient import TestClient
from backend import db,ai_runtime,engine
from backend.providers import PROVIDERS
from backend.models import Profile
from backend.api import app
import backend.api as api_module

P={'summary':'Engineer','title':'Engineer','skills':[],'domains':[],'experience_summary':'Unknown',
   'years_experience':None,'signals':[],'unknowns':[],'contradictions':[]}

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'RUNTIME',tmp_path);db.init()

@pytest.mark.parametrize('provider',list(PROVIDERS))
def test_native_adapters_same_schema_usage_and_no_key_in_url(provider,monkeypatch):
    db.set_setting('provider',provider);db.set_setting('model','test-model')
    db.set_setting('ai_check',{'provider':provider,'model':'test-model','ok':True})
    monkeypatch.setattr(ai_runtime,'key',lambda *a:'TEST_SECRET')
    requests=[]
    bodies={
        'deepseek':{'choices':[{'message':{'content':json.dumps(P)}}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}},
        'openai':{'status':'completed','output':[{'type':'reasoning'},{'type':'message','content':[{'type':'output_text','text':json.dumps(P)}]}],'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}},
        'anthropic':{'stop_reason':'tool_use','content':[{'type':'thinking','thinking':'not returned'}, {'type':'tool_use','name':'emit_result','input':P}],'usage':{'input_tokens':8,'cache_read_input_tokens':2,'output_tokens':5}},
        'gemini':{'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'thought','thought':True},{'text':json.dumps(P)}]}}],'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':3,'thoughtsTokenCount':2,'totalTokenCount':15}}
    }
    class Client:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,url,**kw):
            requests.append((url,kw))
            return httpx.Response(200,json=bodies[provider],request=httpx.Request('POST',url))
    monkeypatch.setattr(ai_runtime.httpx,'AsyncClient',Client)
    assert asyncio.run(ai_runtime.ai('profile',{'test':'INPUT'},Profile))=={**P,'facts':[]}
    call=db.unpack(db.one('SELECT * FROM calls'),('usage',))
    assert call['provider']==provider and call['usage']['total_tokens']==15
    url,req=requests[0];assert 'TEST_SECRET' not in url
    assert 'TEST_SECRET' not in json.dumps(req['json']) and 'TEST_SECRET' not in json.dumps(call)
    if provider=='openai':assert req['json']['store'] is False and 'temperature' not in req['json']
    if provider=='anthropic':assert req['json']['tool_choice']=={'type':'auto'}

@pytest.mark.parametrize('provider', ['anthropic','gemini'])
def test_model_catalog_pagination(provider):
    requests=[]
    class Client:
        async def get(self,url,**kw):
            requests.append(kw)
            if provider=='anthropic':
                data={'data':[{'id':'claude-one' if len(requests)==1 else 'claude-two'}],
                      'has_more':len(requests)==1,'last_id':'claude-one'}
            else:
                data={'models':[{'name':'models/gemini-one' if len(requests)==1 else 'models/gemini-two',
                                 'supportedGenerationMethods':['generateContent']},
                                 {'name':'models/embedding','supportedGenerationMethods':['embedContent']}],
                      **({'nextPageToken':'next'} if len(requests)==1 else {})}
            return httpx.Response(200,json=data,request=httpx.Request('GET',url))
    result=asyncio.run(PROVIDERS[provider].models(Client(),'TEST'))
    assert len(result)==2 and len(requests)==2
    assert requests[1]['params'].get('after_id' if provider=='anthropic' else 'pageToken')

def test_credentials_isolated_and_never_returned(monkeypatch,tmp_path):
    for p in PROVIDERS.values():monkeypatch.delenv(p.key_env,raising=False)
    monkeypatch.setattr(ai_runtime,'ENV_FILE',tmp_path/'missing.env')
    f=tmp_path/'customer.env';f.write_text('OPENAI_API_KEY=TEST_OPENAI\nGEMINI_API_KEY=TEST_GEMINI',encoding='utf-8')
    db.set_setting('env_file:openai',str(f));db.set_setting('env_file:gemini',str(f))
    assert ai_runtime.key('openai')=='TEST_OPENAI' and ai_runtime.key('gemini')=='TEST_GEMINI'
    assert not ai_runtime.configured('anthropic')
    assert 'TEST_OPENAI' not in json.dumps(ai_runtime.provider_catalog())

def test_public_json_retry_does_not_echo_malformed_output(monkeypatch):
    from backend.search_schema import PublicJudgeBatch
    db.set_setting('provider','deepseek');db.set_setting('model','deepseek-flash')
    db.set_setting('ai_check',{'provider':'deepseek','model':'deepseek-flash','ok':True})
    monkeypatch.setattr(ai_runtime,'key',lambda *a:'TEST')
    valid={'items':[{'id':'PUB001','a':[{'c':'C1','s':'UNKNOWN','why':'Unknown','q':[]}]}]}
    malformed='{"items":['
    prompts=[];rejected=[]
    class Adapter:
        async def generate(self,*args):
            prompts.append(args[4])
            return (malformed if len(prompts)==1 else json.dumps(valid)),{'total_tokens':10}
    monkeypatch.setattr(ai_runtime,'adapter',lambda *a:Adapter())
    result=asyncio.run(ai_runtime.ai('public_content_assessment',{},PublicJudgeBatch,on_rejected=rejected.append))
    assert result['items'][0]['candidate_id']=='PUB001' and len(prompts)==2
    assert len(rejected)==1 and rejected[0]['output']==malformed
    assert 'Previous rejected JSON' not in prompts[1] and 'exactly one complete JSON object' in prompts[1]

def test_selected_customer_key_file_never_falls_back_to_another_account(monkeypatch,tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY','DIFFERENT_ACCOUNT_ENV')
    f=tmp_path/'customer.env';f.write_text('OPENAI_API_KEY=CUSTOMER_ACCOUNT',encoding='utf-8')
    assert ai_runtime.key('openai',str(f))=='CUSTOMER_ACCOUNT'
    with pytest.raises(ai_runtime.IntegrationError):ai_runtime.key('openai',str(tmp_path/'missing.env'))
    assert ai_runtime.key('openai','')=='DIFFERENT_ACCOUNT_ENV'

def test_provider_switch_is_atomic_history_retained_and_queue_guard(monkeypatch):
    db.set_setting('provider','deepseek');db.set_setting('model','old-model');db.set_setting('ai_check',{'ok':True})
    db.execute("INSERT INTO runs(id,job_id,status,model,provider,created) VALUES('old','IT-1','COMPLETED','old-model','deepseek','now')")
    async def check(provider,model,**kw):return {'ok':model=='new-model','provider':provider,'model':model,'error':'Unavailable'}
    monkeypatch.setattr(api_module,'check_ai',check)
    c=TestClient(app);headers={'X-Pilot-Request':'1'}
    assert c.put('/api/settings/ai',headers=headers,json={'provider':'openai','model':'bad','env_file':'missing'}).status_code==409
    assert db.setting('provider')=='deepseek' and db.setting('env_file:openai') is None
    assert c.put('/api/settings/ai',headers=headers,json={'provider':'openai','model':'new-model'}).status_code==200
    assert db.setting('provider')=='openai' and c.get('/api/runs').json()[0]['provider']=='deepseek'
    engine.enqueue('sync',{})
    assert c.put('/api/settings/ai',headers=headers,json={'provider':'gemini','model':'new-model'}).status_code==409
    assert db.setting('provider')=='openai'

def test_legacy_schema_migration_labels_history_deepseek(tmp_path):
    with sqlite3.connect(tmp_path/'pilot.sqlite3') as c:
        c.execute('DROP TABLE calls')
        c.execute('CREATE TABLE calls(id INTEGER PRIMARY KEY,task TEXT,model TEXT,prompt_version TEXT,usage TEXT,status TEXT,error TEXT,created TEXT)')
        c.execute("INSERT INTO calls VALUES(1,'profile','deepseek-flash','pilot-1.0','{}','COMPLETED',NULL,'old')")
    db.init()
    assert db.one('SELECT * FROM calls')['provider']=='deepseek'

def test_refusal_usage_is_metered_without_fallback(monkeypatch):
    db.set_setting('provider','openai');db.set_setting('model','test-model')
    db.set_setting('ai_check',{'provider':'openai','model':'test-model','ok':True})
    monkeypatch.setattr(ai_runtime,'key',lambda *a:'TEST')
    urls=[]
    class Client:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,url,**kw):
            urls.append(url)
            return httpx.Response(200,json={'status':'incomplete','output':[], 'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}},request=httpx.Request('POST',url))
    async def sleep(*a):pass
    monkeypatch.setattr(ai_runtime.httpx,'AsyncClient',Client);monkeypatch.setattr(ai_runtime.asyncio,'sleep',sleep)
    with pytest.raises(ai_runtime.IntegrationError):asyncio.run(ai_runtime.ai('profile',{},Profile))
    assert len(urls)==3 and all('api.openai.com' in url for url in urls)
    assert sum(json.loads(c['usage'])['total_tokens'] for c in db.rows('SELECT usage FROM calls'))==45
