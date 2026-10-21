import pytest
from fastapi.testclient import TestClient
from test_engine import database
from backend import db,pipeline as p
from backend.errors import IntegrationError
from backend.api import app

def test_validated_dependency_idempotence_and_versions():
    rid=p.run('assessment','fixture')
    source=p.artifact('source',payload={'revision':'r1'})
    node=p.begin(rid,'evaluate',{'source':source},{'model':'one'})
    assert node==p.begin(rid,'evaluate',{'source':source},{'model':'one'})
    output=p.finish(node,'judgment',{'status':'UNKNOWN'},lambda v:None)
    assert p.ingest(output,'judgment')['status']=='UNKNOWN'
    assert node==p.begin(rid,'evaluate',{'source':source},{'model':'one'})
    assert node!=p.begin(rid,'evaluate',{'source':source},{'model':'two'})
    changed=p.artifact('source',payload={'revision':'r2'})
    assert node!=p.begin(rid,'evaluate',{'source':changed},{'model':'one'})
    with pytest.raises(IntegrationError):p.ingest(output,'source')

def test_rejected_output_debug_only_and_retry():
    rid=p.run('assessment','bad');node=p.begin(rid,'node',{}, {})
    def reject(v):raise ValueError('wrong revision')
    with pytest.raises(IntegrationError):p.finish(node,'judgment',{'bad':True},reject)
    report=p.manifest(rid);link=report['attempts'][0]['links'][0]
    assert p.read_artifact(rid,link['id'])['payload']=={'bad':True}
    with pytest.raises(IntegrationError):p.ingest(link['id'])
    assert p.output(node) is None
    assert p.begin(rid,'node',{}, {})!=node

def test_hash_and_schema_corruption_blocks_ingest():
    aid=p.artifact('source',payload={'revision':'r1'})
    db.execute("UPDATE artifacts SET content_hash='bad' WHERE id=?",(aid,))
    with pytest.raises(IntegrationError):p.ingest(aid)
    db.execute("UPDATE artifacts SET schema_version='future' WHERE id=?",(aid,))
    with pytest.raises(IntegrationError):p.ingest(aid)

def test_long_text_references_export_and_ownership():
    text='Kinh nghiệm Python\n'*200
    rid=p.run('assessment','one');other=p.run('assessment','two')
    _,aid=p.capture(rid,'content',{}, {},'source-content',{'text':text,'evidence':{'P1':'Kinh nghiệm Python'}})
    frozen=p.ingest(aid)
    assert '$text' in frozen['text'] and '$slice' in frozen['evidence']['P1']
    assert p.thaw(frozen)['text']==text
    exported=p.export(rid)
    assert sum(a['body']==text for a in exported['artifacts'])==1
    client=TestClient(app)
    assert client.get(f'/api/v1/pipeline-runs/{other}/artifacts/{aid}').status_code==404
    assert client.get(f'/api/v1/pipeline-runs/{rid}/export').status_code==200

def test_finish_transaction_rolls_back(monkeypatch):
    rid=p.run('assessment','crash');node=p.begin(rid,'node',{}, {})
    original=p._artifact
    def crash(*a,**k):original(*a,**k);raise RuntimeError('crash before commit')
    monkeypatch.setattr(p,'_artifact',crash)
    with pytest.raises(RuntimeError):p.finish(node,'value',{'ok':True},lambda _:None)
    assert db.one('SELECT status FROM node_attempts WHERE id=?',(node,))['status']=='RUNNING'
    assert not db.rows("SELECT * FROM artifact_links WHERE attempt_id=? AND direction='OUTPUT'",(node,))
    monkeypatch.setattr(p,'_artifact',original)
    p.finish(node,'value',{'ok':True},lambda _:None)
    assert p.output(node)=={'ok':True}

def test_export_redacts_auth_and_private_note():
    rid=p.run('assessment','safe')
    p.capture(rid,'record',{}, {},'debug',{'headers':{'Authorization':'SECRET'},'private_note':'PRIVATE','value':'public'})
    text=p.canonical(p.export(rid))
    assert 'SECRET' not in text and 'PRIVATE' not in text
    assert 'public' in text and '[REDACTED]' in text
