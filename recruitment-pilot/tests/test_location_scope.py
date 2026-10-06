import asyncio,json
import pytest
from backend import db,exa_search as exa,people_search as people
from backend.location_scope import effective_query,PREFIXES
from backend.errors import IntegrationError
from test_people import setup
from test_engine import database,fake_ai
from test_exa import mock_api,PAYLOAD

@pytest.mark.parametrize('scope',list(PREFIXES))
def test_direct_preview_wire_cache_and_restart(scope,monkeypatch):
    monkeypatch.setenv('EXA_API_KEY','test-key');calls=mock_api(monkeypatch,[(200,PAYLOAD)])
    s=exa.create('Sitecore engineers',mode='people',location_scope=scope)
    assert s['effective_query']==effective_query('Sitecore engineers',scope)
    assert exa.create('Sitecore engineers',mode='people',location_scope=scope)['id']==s['id']
    asyncio.run(exa.run(s['id']))
    assert calls[0][1]['json']['query']==PREFIXES[scope]+'Sitecore engineers'
    assert len(calls)==1
    db.init();assert exa.detail(s['id'])['location_scope']==scope

def test_legacy_and_scope_cache_separate(monkeypatch):
    monkeypatch.setenv('EXA_API_KEY','test-key')
    old=exa.create('Sitecore engineers',mode='people')
    assert old['location_scope'] is None and old['effective_query']=='Sitecore engineers'
    ids={old['id']}|{exa.create('Sitecore engineers',mode='people',location_scope=s)['id'] for s in PREFIXES}
    assert len(ids)==4
    with pytest.raises(IntegrationError):exa.create('Sitecore engineers',mode='web',location_scope='VIETNAM')
    for scope in ('bad','VIETNAM'):
        with pytest.raises(IntegrationError):exa.create('X'*1500,mode='people',location_scope=scope)
    with pytest.raises(IntegrationError):exa.create('Contact private@example.com',mode='people',location_scope='VIETNAM')

def test_group_scope_selected_only_no_extra_ai(fake_ai,monkeypatch):
    version=setup(fake_ai,monkeypatch);before=len(fake_ai)
    original=db.one("SELECT data FROM configs WHERE job_id='IT-1'")['data']
    calls=mock_api(monkeypatch,[(200,PAYLOAD)])
    g=people.create('IT-1',version,['S1'],location_scope='VIETNAM')
    assert g['snapshot']['location_scope']=='VIETNAM' and len(g['items'])==1
    assert g['items'][0]['strategy']['effective_query'].startswith(PREFIXES['VIETNAM'])
    asyncio.run(exa.run(g['items'][0]['search']['id']))
    assert len(calls)==1 and len(fake_ai)==before
    assert db.one("SELECT data FROM configs WHERE job_id='IT-1'")['data']==original
    assert people.create('IT-1',version,['S1'],location_scope='VIETNAM')['id']==g['id']
    outside=people.create('IT-1',version,['S1'],location_scope='INTERNATIONAL')
    assert outside['id']!=g['id']
    assert people.detail(g['id'])['snapshot']['location_scope']=='VIETNAM'
    assert not db.rows("SELECT id FROM profiles WHERE kind='candidate'")

def test_preview_frontend_prefixes_match_backend():
    from pathlib import Path
    ui=Path('frontend/src/LocationScope.tsx').read_text(encoding='utf-8')
    assert all(prefix in ui for prefix in PREFIXES.values())

def test_scope_not_in_professional_score():
    from backend.assessment_scoring import score
    from test_engine import CONFIG
    item={'candidate_id':'PUB001','assessments':[{'criterion_id':'C1','status':'MET'},{'criterion_id':'C2','status':'UNKNOWN'}]}
    results=[score({**CONFIG,'location_scope':s},item,1) for s in PREFIXES]
    assert all(r['score']==75 and r['coverage']==75 for r in results)
