import pytest
from backend import shortlist_review as review
from backend.errors import IntegrationError
from test_engine import database

def test_blind_sample_persists_and_labels_are_versioned(database,monkeypatch):
    rows=[{'canonical_candidate_id':str(i),'url':f'https://example.com/{i}',
           'title':'Person','source_urls':[f'https://example.com/{i}'],
           'assessment_id':'A'+str(i),'child_source_id':'PUB001','score':100-i} for i in range(20)]
    monkeypatch.setattr(review.assessment_groups,'detail',lambda _: {'results':rows,'config_version':3,'job_id':'J'})
    monkeypatch.setattr(review.assessment_groups,'row',lambda _: {'snapshot':{'sources':[{'url':'https://vn.linkedin.com/in/known'}]}})
    first=review.blind('G')
    assert len(first['items'])==15
    assert all('score' not in r and 'top10' not in r for r in first['items'])
    cid=first['items'][0]['id']
    review.label('G',cid,'CONTACT','Relevant experience',['C1'],0)
    with pytest.raises(IntegrationError):review.label('G',cid,'VERIFY','stale',[],0)
    monkeypatch.setattr(review.assessment_groups,'detail',lambda _: (_ for _ in ()).throw(AssertionError('sample rebuilt')))
    assert review.blind('G')['items']==first['items']
    assert review.metrics('G')['reviewed']==1
    assert not review.metrics('G')['quality_accepted']
    result=review.add_control('G','https://linkedin.com/in/known','Known relevant professional')
    assert result['controls'][0]['found_in_search']
    with pytest.raises(IntegrationError):review.add_control('G','https://www.linkedin.com/in/known/','Duplicate')
