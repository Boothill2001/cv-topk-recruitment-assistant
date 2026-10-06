import asyncio,json
import httpx,pytest
from backend.judge_stream import CandidateStream
from backend.providers import DeepSeek
from backend import db,ai_runtime,ai_assessment,search_orchestration as service
from backend.search_schema import JudgeBatch
from test_engine import database,fake_ai
from test_batch_search import setup,batch

@pytest.mark.parametrize('step',[1,7,10000])
def test_parser_emits_only_complete_objects_once(step):
    items=[{'id':'CV1','a':[{'why':'trích dẫn } , \\"items\\": [ và \\n','s':'UNKNOWN'}]},{'id':'CV2','a':[]}]
    text=json.dumps({'items':items},ensure_ascii=False);p=CandidateStream();received=[]
    for i in range(0,len(text),step):received.extend(p.feed(text[i:i+step]))
    assert received==items
    assert p.feed(' ')==[]
    incomplete=CandidateStream();assert incomplete.feed('{"items":[{"id":"CV1"')==[]

def test_deepseek_stream_delta_usage_and_truncation():
    observed=[]
    class Response:
        def raise_for_status(self):pass
        async def aiter_lines(self):
            yield ': keepalive'
            for delta in ('{"items":[','{"id":"CV1","a":[]}',']}'):
                yield 'data: '+json.dumps({'choices':[{'delta':{'content':delta},'finish_reason':None}]})
            yield 'data: '+json.dumps({'choices':[{'delta':{},'finish_reason':'stop'}]})
            yield 'data: '+json.dumps({'choices':[],'usage':{'total_tokens':123}})
            yield 'data: [DONE]'
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    class Client:
        def stream(self,*a,**kw):
            assert kw['json']['stream'] and kw['json']['stream_options']['include_usage']
            return Response()
    text,usage=asyncio.run(DeepSeek().generate_stream(Client(),'TEST','model','sys','input',{},1000,observed.append))
    assert json.loads(text)['items'][0]['id']=='CV1' and usage['total_tokens']==123 and len(observed)==3

def test_partial_validated_reset_on_retry_and_final_exact(fake_ai,monkeypatch):
    v=setup(fake_ai,3);sid=service.create('IT-1',v)
    async def ai(task,payload,schema,validate,**kw):
        kw['on_attempt']()
        result=batch(payload)
        first=result['items'][0]
        def wire(p):return {'candidate_id':p['candidate_id'],'assessments':[{k:a[k] for k in ('criterion_id','status','explanation','evidence_refs')} for a in p['assessments']]}
        assert kw['on_candidate'](wire(first))
        d=service.detail(sid);assert len(d['preview'])==1 and d['results']==[]
        assert not kw['on_candidate'](wire(first))
        bad=wire(result['items'][1]);bad['assessments'][0]['evidence_refs']=['invented']
        assert not kw['on_candidate'](bad)
        kw['on_usage']({},'FAILED');assert service.detail(sid)['preview']==[]
        kw['on_attempt']();assert service.detail(sid)['preview']==[]
        for p in result['items']:assert kw['on_candidate'](wire(p))
        raw={'items':[wire(p) for p in result['items']]};validate(raw);kw['on_usage']({},'COMPLETED');return raw
    monkeypatch.setattr(ai_runtime,'ai',ai);asyncio.run(service.assess(sid))
    d=service.detail(sid);assert d['preview']==[] and len(d['results'])==3 and d['attempts']==2
    assert all('evidence' in a for r in d['results'] for a in r['assessments'])
    assert all(r['assessments'][0]['evidence'] for r in d['results'])

def test_failed_whole_batch_discards_preview(fake_ai,monkeypatch):
    from backend.errors import IntegrationError
    v=setup(fake_ai,2);sid=service.create('IT-1',v)
    async def ai(task,payload,schema,validate,**kw):
        kw['on_attempt']();first=batch(payload)['items'][0]
        raw={'candidate_id':first['candidate_id'],'assessments':[{k:a[k] for k in ('criterion_id','status','explanation','evidence_refs')} for a in first['assessments']]}
        assert kw['on_candidate'](raw)
        raise IntegrationError('EOF')
    monkeypatch.setattr(ai_runtime,'ai',ai)
    with pytest.raises(IntegrationError):asyncio.run(service.assess(sid))
    d=service.detail(sid);assert d['preview']==[] and d['results']==[] and d['status']=='FAILED'


def test_terminal_sse_is_read_only_and_closes(fake_ai,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app
    v=setup(fake_ai,2);sid=service.create('IT-1',v)
    async def ai(task,payload,schema,validate,**kw):
        kw['on_attempt']();r=batch(payload);validate(r);return r
    monkeypatch.setattr(ai_runtime,'ai',ai);asyncio.run(service.assess(sid))
    before=db.one('SELECT COUNT(*) n FROM tasks')['n']
    with TestClient(app) as client:
        response=client.get('/api/v1/searches/'+sid+'/events')
        assert response.headers['content-type'].startswith('text/event-stream')
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert len(events)==1 and events[0]['status']=='COMPLETED' and len(events[0]['results'])==2
        assert client.get('/api/v1/searches/absent/events').status_code==404
    assert db.one('SELECT COUNT(*) n FROM tasks')['n']==before
