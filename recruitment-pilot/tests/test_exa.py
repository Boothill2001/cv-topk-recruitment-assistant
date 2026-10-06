import asyncio
import httpx
import pytest
from backend import db, exa_search as exa
from backend.errors import IntegrationError

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'RUNTIME',tmp_path)
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('EXA_API_KEY','test-secret-not-real')
    db.init()

def mock_api(monkeypatch,responses):
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            calls.append((url,kwargs))
            status,payload=responses[min(len(calls)-1,len(responses)-1)]
            return httpx.Response(status,json=payload,request=httpx.Request('POST',url))
    monkeypatch.setattr(exa.httpx,'AsyncClient',Client)
    async def no_wait(*args):pass
    monkeypatch.setattr(exa.asyncio,'sleep',no_wait)
    return calls

PAYLOAD={'requestId':'req-1','costDollars':{'total':0.007},'results':[
    {'title':'Example','url':'https://example.com/page','highlights':['A source excerpt']},
    {'title':'Duplicate','url':'https://example.com/page#section'},
    {'title':'Unsafe','url':'javascript:alert(1)'}]}

def test_minimal_request_cache_and_refresh(monkeypatch):
    calls=mock_api(monkeypatch,[(200,PAYLOAD)])
    first=exa.create('Public search query')
    assert exa.create('Public search query')['id']==first['id']
    asyncio.run(exa.run(first['id']))
    result=exa.detail(first['id'])
    assert result['status']=='COMPLETED' and len(result['results'])==1
    assert result['omitted_count']==2 and result['known_cost_dollars']==0.007
    assert calls[0][1]['json']=={'query':'Public search query','type':'auto','contents':{'highlights':True}}
    assert 'test-secret' not in db.dumps(result)
    assert exa.create('Public search query')['id']==first['id']
    asyncio.run(exa.run(first['id']))
    assert len(calls)==1
    assert exa.create('Public search query',refresh=True)['id']!=first['id']

@pytest.mark.parametrize('status,expected',[(401,1),(429,3),(503,3)])
def test_error_limits_and_sanitization(monkeypatch,status,expected):
    calls=mock_api(monkeypatch,[(status,{'error':'test-secret-not-real'})])
    sid=exa.create('Some public query')['id'];asyncio.run(exa.run(sid))
    r=exa.detail(sid)
    assert r['status']=='FAILED' and r['attempts']==expected and len(calls)==expected
    assert r['has_unreported_cost'] and 'test-secret' not in db.dumps(r)
    if expected==3:
        with pytest.raises(IntegrationError):exa.retry(sid)
        asyncio.run(exa.run(sid));assert len(calls)==3

def test_invalid_response_retains_reported_cost(monkeypatch):
    mock_api(monkeypatch,[(200,{'costDollars':{'total':0.01},'results':'invalid'})])
    sid=exa.create('Some public query')['id'];asyncio.run(exa.run(sid))
    r=exa.detail(sid)
    assert r['status']=='FAILED' and r['known_cost_dollars']==0.01

def test_removed_key_fails_clearly(monkeypatch):
    sid=exa.create('Some public query')['id']
    monkeypatch.delenv('EXA_API_KEY');asyncio.run(exa.run(sid))
    r=exa.detail(sid);assert r['status']=='FAILED' and r['attempts']==0

def test_restart_pending_retained_running_interrupted():
    pending=exa.create('Pending public query')['id'];running=exa.create('Running public query')['id']
    db.execute("UPDATE web_searches SET status='RUNNING',attempts=1 WHERE id=?",(running,))
    db.init()
    assert exa.detail(pending)['status']=='PENDING'
    r=exa.detail(running);assert r['status']=='INTERRUPTED' and r['has_unreported_cost']

def test_missing_cost_remains_unknown(monkeypatch):
    mock_api(monkeypatch,[(200,{'results':[]})])
    sid=exa.create('Some public query')['id'];asyncio.run(exa.run(sid))
    r=exa.detail(sid);assert r['status']=='COMPLETED' and r['has_unreported_cost']
