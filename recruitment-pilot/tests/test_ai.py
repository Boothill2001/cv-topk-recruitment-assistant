import asyncio
import json
import httpx
import pytest
from backend import db,ai_runtime as integrations
from backend.models import Profile

P={'summary':'Engineer','title':'Engineer','skills':[],'domains':[],'experience_summary':'Unknown',
   'years_experience':None,'signals':[],'unknowns':[],'contradictions':[]}

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'RUNTIME',tmp_path);db.init();db.set_setting('ai_check',{'ok':True,'model':'deepseek-flash'})
    monkeypatch.setattr(integrations,'key',lambda *args:'TEST_ONLY_NOT_A_REAL_KEY')

def test_json_retry_correction_and_max_concurrency(monkeypatch):
    active=0;peak=0;count=0
    class Client:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,*a,**kw):
            nonlocal active,peak,count
            active+=1;peak=max(peak,active);count+=1
            await asyncio.sleep(.01);active-=1
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(P)}}], 'usage':{'total_tokens':123}},request=httpx.Request('POST','https://example.invalid'))
    monkeypatch.setattr(integrations.httpx,'AsyncClient',Client)
    async def run():
        monkeypatch.setattr(integrations,'AI_SEMAPHORE',asyncio.Semaphore(2))
        await asyncio.gather(*(integrations.ai('profile',{},Profile) for _ in range(6)))
    asyncio.run(run());assert peak==2 and count==6
    assert sum(json.loads(r['usage'])['total_tokens'] for r in db.rows('SELECT usage FROM calls'))==738

def test_rate_limit_retries_three_times_without_fixture(monkeypatch):
    calls=0
    class Client:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,*a,**kw):
            nonlocal calls
            calls+=1
            return httpx.Response(429,request=httpx.Request('POST','https://example.invalid'))
    async def sleep(*a):pass
    monkeypatch.setattr(integrations.httpx,'AsyncClient',Client)
    monkeypatch.setattr(integrations.asyncio,'sleep',sleep)
    with pytest.raises(integrations.IntegrationError):asyncio.run(integrations.ai('profile',{},Profile))
    assert calls==3
    assert all(r['status']=='FAILED' for r in db.rows('SELECT status FROM calls'))

def test_invalid_json_is_retried_with_schema_feedback(monkeypatch):
    requests=[]
    class Client:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,*a,**kw):
            requests.append(kw['json'])
            data=P if len(requests)>1 else {'summary':'bad missing fields'}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(data)}}], 'usage':{'total_tokens':1}},request=httpx.Request('POST','https://example.invalid'))
    async def sleep(*a):pass
    monkeypatch.setattr(integrations.httpx,'AsyncClient',Client);monkeypatch.setattr(integrations.asyncio,'sleep',sleep)
    assert asyncio.run(integrations.ai('profile',{},Profile))['title']=='Engineer'
    assert len(requests)==2 and 'Lần trả JSON trước' in requests[1]['messages'][1]['content']
