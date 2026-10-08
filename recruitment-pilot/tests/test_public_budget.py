from backend import public_assessment as service, ai_assessment
from backend.errors import IntegrationError
import pytest

def test_preview_and_create_share_limits(monkeypatch):
    monkeypatch.setattr(ai_assessment,'model_budget',lambda:{'verified':True,'context_chars':250000,'output_tokens':32000,'max_cells':300,'model_context_tokens':1000000,'model_max_output_tokens':32000,'public_context_chars':100000})
    base={'public_jd':['JD'],'criteria':[{'id':'C1'}],'sources':[]}
    for count in (3,7,10):
        snap={**base,'sources':[{'id':str(i),'evidence':{'E1':'Kỹ năng chuyên môn '*100}} for i in range(count)]}
        result=service.budget_preview(snap)
        assert result['fits'] and result['usage_estimate']['source_count']==count
        assert result['usage_estimate']['token_count_method']=='conservative_upper_bound'
        assert service.preflight(snap)==result['usage_estimate']['reserved_output_tokens']
    huge={**base,'sources':[{'evidence':{'E1':'x'*1100000}}]}
    assert 'context_tokens' in [v['code'] for v in service.budget_preview(huge)['violations']]
    with pytest.raises(IntegrationError):service.preflight(huge)

def test_output_and_unknown_model(monkeypatch):
    caps={'verified':True,'context_chars':250000,'output_tokens':512,'max_cells':300,'model_context_tokens':1000000}
    monkeypatch.setattr(ai_assessment,'model_budget',lambda:caps)
    snap={'public_jd':[],'criteria':[{'id':str(i)} for i in range(7)],'sources':[{'id':str(i)} for i in range(10)],'content_fetch_id':'F'}
    assert 'output_tokens' in [v['code'] for v in service.budget_preview(snap)['violations']]
    caps['verified']=False
    assert 'unverified_model_budget' in [v['code'] for v in service.budget_preview(snap)['violations']]

from test_engine import database, fake_ai
from test_public_assessment import setup
from backend import db
import json

def test_exact_urls_preview_does_not_queue(fake_ai):
    version=setup(fake_ai)
    row=db.one('SELECT response FROM web_searches')
    response=json.loads(row['response']);seed=response['results'][0]
    response['results']=[{**seed,'url':f'https://example.com/person{i}','title':str(i)} for i in range(10)]
    db.execute('UPDATE web_searches SET response=?',(db.dumps(response),))
    before=len(db.rows('SELECT id FROM tasks'))
    for count in (3,7,10):
        urls=[f'https://example.com/person{i}' for i in range(10-count,10)]
        result=service.create('IT-1',version,'S',urls,preview=True)
        assert result['eligible_urls']==urls
        assert result['usage_estimate']['source_count']==count
    assert len(db.rows('SELECT id FROM tasks'))==before
